What the image was built from, read by `doomtp_bot.core.build` for `/api/v1/stats` and `$bot.release` /
`$bot.commit`. CI's image job writes `ref` (the tag or branch) and `commit` (the full SHA) here before it
builds; a checkout has neither, and the bot then reports none.
