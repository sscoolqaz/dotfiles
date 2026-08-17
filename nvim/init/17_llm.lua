require('codecompanion').setup({
  adapters = {
    acp = {
      extend = vim.g.llm_acp_extend,
    },
  },
  interactions = {
    inline = { adapter = 'claude_code' },
    cmd = { adapter = 'claude_code' },
    chat = { adapter = 'claude_code' },
  },
  display = { action_palette = {
    provider = 'mini_pick',
  }, diff = {
    provider = 'mini_diff',
  } },
  extensions = {
    spinner = {},
  },
})
