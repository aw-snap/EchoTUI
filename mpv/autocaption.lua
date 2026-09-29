local utils = require 'mp.utils'
local options = require 'mp.options'

-- echotui starts mpv with --script-opts=autocaption-auto=yes to caption without pressing Ctrl+C
local opts = { auto = false }
options.read_options(opts, "autocaption")

local is_windows = package.config:sub(1, 1) == "\\"
local model_path = mp.command_native({ "expand-path", "~~/ggml-small.en.bin" })
local whisper_cmd = "whisper-cli" -- must be on PATH, or put the full path here

local tmp = os.getenv("TEMP") or "/tmp"
-- per-process names so two mpv windows don't clobber each other's files
local tag = tostring(utils.getpid())
local log_path = utils.join_path(tmp, "mpv_whisper_" .. tag .. ".log")
local srt_path = utils.join_path(tmp, "mpv_whisper_" .. tag .. ".srt")
local wav_path = utils.join_path(tmp, "mpv_audio_" .. tag .. ".wav")

local is_processing = false
local timer = nil
local last_pos = 0
local sub_index = 1
local subs_added = false
local offset_ms = 0 -- audio before this point was captioned ahead of time (echotui's pre-pass)
local prefill = "" -- that pre-pass srt, copied into the live file so one track covers the whole video

local function shift(ts)
	local h, m, s, f = ts:match("(%d+):(%d+):(%d+)[,.](%d+)")
	local t = ((tonumber(h) * 60 + tonumber(m)) * 60 + tonumber(s)) * 1000 + tonumber(f) + offset_ms
	return string.format("%02d:%02d:%02d,%03d", math.floor(t / 3600000), math.floor(t / 60000) % 60,
		math.floor(t / 1000) % 60, t % 1000)
end

local function stop_polling()
	if timer then
		timer:kill()
		timer = nil
	end
	is_processing = false
end

local function poll_log()
	local log_file = io.open(log_path, "r")
	if not log_file then return end

	log_file:seek("set", last_pos)
	local changed = false
	local new_subs = ""

	for line in log_file:lines() do
		-- Improved flexible regex to catch timestamps with or without brackets
		-- Matches: [00:00:00.000 --> 00:00:05.000] Text OR 00:00:00.000 --> 00:00:05.000 Text
		local t1, t2, text = line:match("[%[%s]*(%d%d:%d%d:%d%d%.%d%d%d)%s*%-%->%s*(%d%d:%d%d:%d%d%.%d%d%d)[%s%]]*(.*)")

		if t1 and t2 and text then
			t1 = shift(t1)
			t2 = shift(t2)

			new_subs = new_subs .. sub_index .. "\n" .. t1 .. " --> " .. t2 .. "\n" .. text .. "\n\n"
			sub_index = sub_index + 1
			changed = true
		end
	end

	last_pos = log_file:seek()
	log_file:close()

	if changed then
		local srt_file = io.open(srt_path, "a")
		if srt_file then
			srt_file:write(new_subs)
			srt_file:close()

			-- ONLY call sub-add once the first piece of actual text exists
			if not subs_added then
				mp.commandv("sub-add", srt_path)
				subs_added = true
			end
			mp.commandv("sub-reload")
		end
	end
end

local function run_whisper()
	mp.osd_message("Step 2/2: Generating Live AI Subtitles...", 4)

	os.remove(log_path)
	os.remove(srt_path)
	last_pos = 0
	sub_index = 1
	subs_added = false
	if prefill ~= "" then
		local f = io.open(srt_path, "w")
		f:write(prefill)
		f:close()
		for _ in prefill:gmatch("%-%->") do sub_index = sub_index + 1 end
		mp.commandv("sub-add", srt_path)
		subs_added = true
	end

	timer = mp.add_periodic_timer(1, poll_log)

	-- FIX: Added '2>&1' to capture the Vulkan/GPU output stream which often goes to stderr
	local args
	if is_windows then
		-- cmd.exe mangles quoted paths passed on its command line, so run the redirect from a .bat
		local bat_path = utils.join_path(tmp, "mpv_whisper.bat")
		local bat = io.open(bat_path, "w")
		bat:write(string.format('@"%s" -m "%s" -f "%s" > "%s" 2>&1\r\n', whisper_cmd, model_path, wav_path, log_path))
		bat:close()
		args = { "cmd", "/c", bat_path }
	else
		args = { "sh", "-c", string.format("stdbuf -oL '%s' -m '%s' -f '%s' > '%s' 2>&1", whisper_cmd, model_path,
			wav_path, log_path) }
	end

	local cmd2 = {
		name = "subprocess",
		playback_only = false,
		args = args
	}

	mp.command_native_async(cmd2, function(success, result, error)
		poll_log()
		stop_polling()
		if success and result.status == 0 then
			mp.osd_message("Subtitle generation fully completed!", 4)
		else
			mp.osd_message("Subtitle generation stopped or failed.", 4)
		end
	end)
end

local function ffmpeg_args(path)
	local args = { "ffmpeg", "-y" }
	if offset_ms > 0 then
		table.insert(args, "-ss")
		table.insert(args, tostring(offset_ms / 1000))
	end
	for _, a in ipairs({ "-i", path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path }) do
		table.insert(args, a)
	end
	return args
end

local function generate_subs()
	if is_processing then
		mp.osd_message("Subtitles are already processing!", 3)
		return
	end

	local path = mp.get_property("path")
	if not path then
		mp.osd_message("No video loaded.")
		return
	end

	is_processing = true
	mp.osd_message("Step 1/2: Extracting audio to RAM...", 3)
	os.remove(wav_path)

	local cmd1 = {
		name = "subprocess",
		playback_only = false,
		capture_stderr = true,
		args = ffmpeg_args(path)
	}

	mp.command_native_async(cmd1, function(success, result, error)
		if success and result.status == 0 then
			run_whisper()
		else
			-- status < 0 with error_string "init" means ffmpeg couldn't be launched (not on PATH)
			local detail = string.format("status=%s error=%s", tostring(result and result.status),
				tostring(result and result.error_string or error))
			mp.msg.error("ffmpeg failed: " .. detail .. "\n" .. tostring(result and result.stderr))
			mp.osd_message("Failed to extract audio: " .. detail, 8)
			is_processing = false
		end
	end)
end

mp.add_key_binding("ctrl+c", "generate_subs", generate_subs)

if opts.auto then
	mp.register_event("file-loaded", function()
		offset_ms, prefill = 0, ""
		local path = mp.get_property("path")
		local f = path and io.open((path:gsub("%.[^./]+$", ".srt")), "r")
		if f then
			prefill = f:read("*a"):gsub("%s+$", "") .. "\n\n"
			f:close()
			offset_ms = 300000
		end
		generate_subs()
	end)
end
