local utils = require 'mp.utils'
local options = require 'mp.options'

-- echotui starts mpv with --script-opts=autocaption-auto=yes to caption without pressing Ctrl+C
local opts = { auto = false }
options.read_options(opts, "autocaption")

local is_windows = package.config:sub(1, 1) == "\\"
local model_path = mp.command_native({ "expand-path", "~~/ggml-small.en.bin" })
local whisper_cmd = "whisper-cli" -- must be on PATH, or put the full path here
-- -mc 0: no previous text as context, else one line can repeat for the rest of the lecture
local whisper_opts = "-mc 0"

local tmp = os.getenv("TEMP") or "/tmp"
-- per-process names so two mpv windows don't clobber each other's files
local tag = tostring(utils.getpid())
local log_path = utils.join_path(tmp, "mpv_whisper_" .. tag .. ".log")
local tmp_srt = utils.join_path(tmp, "mpv_whisper_" .. tag .. ".srt")
local srt_path = tmp_srt
local wav_path = utils.join_path(tmp, "mpv_audio_" .. tag .. ".wav")

local is_processing = false
local timer = nil
local last_pos = 0
local sub_index = 1
local offset_ms = 0 -- audio before this point is already captioned (echotui's pre-pass or an earlier viewing)
local video = nil -- auto mode: the video whose own .srt srt_path is, appended to rather than replaced
local added = false

local function to_ms(ts)
	local h, m, s, f = ts:match("(%d+):(%d+):(%d+)[,.](%d+)")
	return ((tonumber(h) * 60 + tonumber(m)) * 60 + tonumber(s)) * 1000 + tonumber(f)
end

-- reload the track showing srt_path, or add it (mpv already loaded a video's own .srt by itself)
local function show_subs()
	for _, t in ipairs(mp.get_property_native("track-list")) do
		if t.type == "sub" and t["external-filename"] == srt_path then
			mp.commandv("sub-reload", tostring(t.id))
			return
		end
	end
	if added then
		mp.commandv("sub-reload") -- mpv names the file differently; add it once, not every poll
	else
		mp.commandv("sub-add", srt_path)
		added = true
	end
end

-- carry on from the last caption already in the video's .srt
local function resume()
	offset_ms, sub_index = 0, 1
	local f = io.open(srt_path, "r")
	if not f then return end
	for ts in f:read("*a"):gmatch("%-%->%s*([%d:,.]+)") do
		offset_ms = to_ms(ts)
		sub_index = sub_index + 1
	end
	f:close()
end

local function shift(ts)
	local t = to_ms(ts) + offset_ms
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
	if video and not utils.file_info(video) then
		stop_polling() -- deleted while open: a recreated .srt would hold only the end of the lecture
		return
	end
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
			show_subs()
		end
	end
end

local function run_whisper()
	mp.osd_message("Step 2/2: Generating Live AI Subtitles...", 4)

	os.remove(log_path)
	last_pos = 0
	if not video then
		os.remove(srt_path)
		sub_index = 1
	end

	timer = mp.add_periodic_timer(1, poll_log)

	-- FIX: Added '2>&1' to capture the Vulkan/GPU output stream which often goes to stderr
	local args
	if is_windows then
		-- cmd.exe mangles quoted paths passed on its command line, so run the redirect from a .bat
		local bat_path = utils.join_path(tmp, "mpv_whisper.bat")
		local bat = io.open(bat_path, "w")
		bat:write(string.format('@"%s" %s -m "%s" -f "%s" > "%s" 2>&1\r\n', whisper_cmd, whisper_opts, model_path,
			wav_path, log_path))
		bat:close()
		args = { "cmd", "/c", bat_path }
	else
		args = { "sh", "-c", string.format("stdbuf -oL '%s' %s -m '%s' -f '%s' > '%s' 2>&1", whisper_cmd, whisper_opts,
			model_path, wav_path, log_path) }
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

	if video then
		resume()
		-- ponytail: "done" = captions reach the last minute; a long silent tail gets re-run (quickly) on each open
		local dur = mp.get_property_number("duration")
		if dur and offset_ms >= dur * 1000 - 60000 then
			mp.osd_message("Already captioned", 3)
			return
		end
	else
		offset_ms = 0
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
		-- a file the auto mode can't caption falls back to Ctrl+C on the temp file, never the last lecture's .srt
		video, added, srt_path = nil, false, tmp_srt
		local path = mp.get_property("path")
		local srt = path and path:gsub("%.[^./]+$", ".srt")
		if not srt or srt == path then return end -- no extension: the .srt path would be the video itself
		-- live captions go next to the video, so closing and reopening carries on from where they stopped
		video, srt_path = path, srt
		generate_subs()
	end)
end
