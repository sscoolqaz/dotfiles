require('codecompanion').setup({
  ignore_warnings = true,
  strategies = {
    inline = { adapter = 'claude_code' },
    cmd = { adapter = 'claude_code' },
    chat = { adapter = 'claude_code' },
  },
  display = {
    action_palette = {
      provider = 'mini_pick',
    },
    diff = {
      provider = 'mini_diff',
    },
  },
  extensions = {
    spinner = {},
  },
})
