setopt extendedglob

if [[ -d ~/dev/LunarG_VK ]]; then
    
    latest_dir=$(print -rl ~/dev/LunarG_VK/1.*.*.*(N) | sort -V | tail -n 1)

    export VULKAN_SDK="$latest_dir/x86_64"

    export PATH=$VULKAN_SDK/bin:$PATH
    export LD_LIBRARY_PATH=$VULKAN_SDK/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
    export VK_ADD_LAYER_PATH=$VULKAN_SDK/share/vulkan/explicit_layer.d
    export PKG_CONFIG_PATH=$VULKAN_SDK/share/pkgconfig:$VULKAN_SDK/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}
fi
