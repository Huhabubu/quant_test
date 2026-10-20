"""Fail-closed checks before launching the known modded HeadlessMC installation."""

from pathlib import Path
import re


class PreflightError(ValueError):
    pass


INSTANCE = "neoforge-26.2"


def inspect_installation(mcdir):
    try:
        root = Path(mcdir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreflightError("游戏目录不存在或无法访问") from exc
    instance = root / INSTANCE
    mods = instance / "mods"
    config = instance / "config" / "fml.toml"
    if not instance.is_dir() or not mods.is_dir() or not config.is_file():
        raise PreflightError("未发现现有 neoforge-26.2 实例、mods 或 fml.toml；拒绝启动以避免重新下载")
    names = [p.name.lower() for p in mods.iterdir() if p.is_file() and p.suffix.lower() == ".jar"]
    if len(names) < 20:
        raise PreflightError("启用的 mod JAR 不足 20 个；疑似错误游戏目录，拒绝启动")
    if not any("hmc-specifics" in name for name in names):
        raise PreflightError("缺少 hmc-specifics，拒绝启动")
    if not any("worldboss" in name or "world-boss" in name for name in names):
        raise PreflightError("缺少联调已验证的 World Bosses 客户端 mod，拒绝启动")
    if not any("geckolib" in name for name in names):
        raise PreflightError("缺少联调所需 GeckoLib 客户端 mod，拒绝启动")
    if any("journeymap" in name for name in names):
        raise PreflightError("JourneyMap 在无头模式有已确认崩溃；请先禁用并备份该 mod")
    text = config.read_text(encoding="utf-8")
    if not re.search(r"(?m)^\s*earlyWindowControl\s*=\s*false\s*(?:#.*)?$", text):
        raise PreflightError("fml.toml 未确认 earlyWindowControl=false；拒绝启动")
    return {
        "mcdir": str(root),
        "instance": str(instance),
        "mods_count": len(names),
        "journeymap_enabled": False,
        "early_window_disabled": True,
    }


def build_java_argv(command, mcdir):
    """Force the intended MC directory in the *launcher JVM*, before -jar."""
    args = list(command)
    if len(args) < 3 or args.count("-jar") != 1:
        raise PreflightError("要求明确的 java ... -jar headlessmc.jar 命令")
    jar_idx = args.index("-jar")
    if jar_idx < 1 or jar_idx + 1 != len(args) - 1 or Path(args[-1]).name != "headlessmc.jar":
        raise PreflightError("命令必须只启动 HeadlessMC 启动器，不得附加 launch 等游戏指令")
    for arg in args[1:jar_idx]:
        if arg.startswith("-Dhmc.mcdir=") or arg.startswith("-Dhmc.files.mc="):
            raise PreflightError("请用 --mcdir 设置唯一游戏目录，不允许重复或冲突的 JVM 属性")
    # Set launcher console charset explicitly. Actual in-game Chinese still requires verification.
    prefix = [
        "-Dhmc.mcdir=" + mcdir,
        "-Dhmc.files.mc=" + mcdir,
        "-Dfile.encoding=UTF-8",
        "-Dstdin.encoding=UTF-8",
        "-Dstdout.encoding=UTF-8",
        "-Dstderr.encoding=UTF-8",
    ]
    return args[:jar_idx] + prefix + args[jar_idx:]
