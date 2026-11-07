# Transcription recipes

## Long audio

1. Measure the file: `ffprobe -v error -show_entries format=duration,size -of csv=p=0 talk.wav` gives duration in seconds and size in bytes.
2. When only the size exceeds the model's limit in the API notes, convert to mono 16 kHz in an accepted compressed format such as MP3 or OGG, then measure again: `ffmpeg -i talk.wav -ac 1 -ar 16000 -b:a 64k talk.mp3`.
3. When the duration exceeds the limit, or the converted file still exceeds the size limit, split the audio into parts within both limits, at silences where possible or at a fixed length calculated from the limits and bitrate: `ffmpeg -i talk.mp3 -f segment -segment_time 600 -c copy part-%03d.mp3`, replacing 600 with that length. Measure each part's size and duration, and record its start offset in the source timeline.
4. Transcribe each part with the same model and settings. When merging timings, add each part's start offset to its segment and word times.

## Subtitles and per-word output

Build subtitles and per-word files from the segments and words in the returned JSON. Keep each cue short, splitting long segments at punctuation, and use word timings for split points when the model returns them.
