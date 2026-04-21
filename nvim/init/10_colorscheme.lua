require('catppuccin').setup({
  flavour = 'frappe',
  background = {
    light = 'latte',
    dark = 'mocha',
  },
  transparent_background = true,
  float = {
    transparent = false,
    solid = true,
  },
  dim_inactive = {
    enabled = true,
    shade = 'dark',
    percentage = 0.15,
  },
  default_integrations = true,
  auto_integrations = false,
  integrations = {
    blink_cmp = true,
    nvimtree = true,
    markview = false,
    mason = false,
    mini = {
      enabled = true,
      indentscope_color = '',
    },
  },
})

vim.cmd.colorscheme('catppuccin-nvim')
