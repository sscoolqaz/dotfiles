vim.filetype.add({
  pattern = {
    ['/etc/libvirt/qemu/.*%.xml'] = 'libvirt_xml',
  },
})
