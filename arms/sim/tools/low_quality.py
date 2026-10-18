"""Low-quality / low-GPU RTX preset for local sim windows and test renders (Isaac Sim 6.x carb settings)."""


def apply(fps_cap: int = 30) -> None:
    import carb.settings

    s = carb.settings.get_settings()
    for key, val in {
        "/rtx/post/dlss/execMode": 0,  # DLSS performance (render at a fraction, upscale)
        "/rtx/reflections/enabled": False,
        "/rtx/indirectDiffuse/enabled": False,  # no bounce lighting
        "/rtx/ambientOcclusion/enabled": False,
        "/rtx/translucency/enabled": False,
        "/rtx/raytracing/subsurface/enabled": False,
        "/rtx/caustics/enabled": False,
        "/rtx/directLighting/sampledLighting/enabled": False,
        "/rtx/shadows/enabled": True,  # keep contact shadows so things don't look floating
        "/rtx/post/motionblur/enabled": False,
        "/rtx/post/dof/enabled": False,
        "/rtx/post/bloom/enabled": False,
        "/app/runLoops/main/rateLimitEnabled": True,
        "/app/runLoops/main/rateLimitFrequency": fps_cap,
        "/app/renderer/skipWhileMinimized": True,
    }.items():
        s.set(key, val)
