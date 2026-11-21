# -*- coding: utf-8 -*-
"""ComfyUI 低显存自适应启动器（打包为 ComfyUI启动器.exe）

功能与 comfyui_launcher.ps1 对齐：
  - 自检 torch / CUDA / xformers
  - 按显存档位选择 --vram-headroom 或 --reserve-vram
  - 6G 档自动启用 --fp8_e4m3fn-text-enc
  - 端口占用自动顺延
  - 透传任意 ComfyUI 原生参数
  - --install-env 创建 venv 并安装依赖
"""
import ctypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 本地内置超级账号：可用环境变量覆盖（云服务器部署时务必修改）
ACCOUNT_USER = os.environ.get("COMFY_ADMIN_USER", "admin")
ACCOUNT_PASS = os.environ.get("COMFY_ADMIN_PASS", "admin@123")
IS_WINDOWS = os.name == "nt"

# 官方源在大陆经常卡在 0 速度，优先走阿里云 PyTorch wheel 镜像
TORCH_MIRROR_TPL = "https://mirrors.aliyun.com/pytorch-wheels/{tag}/"
TORCH_OFFICIAL_TPL = "https://download.pytorch.org/whl/{tag}"
# 清华源在部分网络下不可达，优先阿里云，失败回退官方 PyPI
PYPI_MIRRORS = ["https://mirrors.aliyun.com/pypi/simple/", "https://pypi.org/simple"]

PROBE_CODE = """import sys
try:
    import torch
except Exception:
    print("TORCH_MISSING")
    sys.exit(0)
print("TORCH=" + torch.__version__)
print("CUDA=" + ("1" if torch.cuda.is_available() else "0"))
if torch.cuda.is_available():
    print("DEV=" + torch.cuda.get_device_name(0))
try:
    import xformers
    import xformers.ops as _xops
    ok = getattr(_xops, "memory_efficient_attention", None) and getattr(xformers, "_has_cpp_library", True)
    print("XF=" + (xformers.__version__ if ok else "broken"))
except Exception:
    print("XF=none")
"""


def setup_console():
    try:
        if IS_WINDOWS:
            os.system("chcp 65001 >nul 2>&1")
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_app_dir(base):
    # Linux 以脚本方式运行（launcher_src/launcher.py）时，ComfyUI 根目录是上一级
    for candidate in (base, os.path.dirname(base), os.getcwd()):
        if candidate and os.path.exists(os.path.join(candidate, "main.py")):
            return candidate
    try:
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isdir(path) and os.path.exists(os.path.join(path, "main.py")):
                return path
    except OSError:
        pass
    return None


def find_python(app_dir):
    candidates = [
        os.path.join(app_dir, "venv", "Scripts", "python.exe"),
        os.path.join(app_dir, "venv", "bin", "python"),
        os.path.join(app_dir, "python_embeded", "python.exe"),
        shutil.which("python3"),
        shutil.which("python"),
        shutil.which("py"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def total_ram_gb():
    try:
        if not IS_WINDOWS:
            with open("/proc/meminfo", "r", encoding="utf-8") as fp:
                for line in fp:
                    if line.startswith("MemTotal:"):
                        return round(int(line.split()[1]) / (1024 ** 2), 1)
            return 0.0
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return round(status.ullTotalPhys / (1024 ** 3), 1)
    except Exception:
        pass
    return 0.0


def gpu_memory():
    """返回 (总显存MB, 已用MB)，失败返回 (0, 0)。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW,
        )
        if out.returncode == 0 and out.stdout.strip():
            parts = out.stdout.strip().splitlines()[0].split(",")
            total = int(float(parts[0].strip()))
            used = int(float(parts[1].strip())) if len(parts) > 1 else 0
            return total, used
    except Exception:
        pass
    return 0, 0


def probe_env(python):
    info = {"torch": None, "cuda": False, "device": "", "xformers": "none"}
    try:
        out = subprocess.run(
            [python, "-c", PROBE_CODE], capture_output=True, text=True, timeout=180,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return info
    for line in out.stdout.splitlines():
        if line.startswith("TORCH="):
            info["torch"] = line[6:].split("+")[0].strip()
        elif line.startswith("CUDA="):
            info["cuda"] = line[5:].strip() == "1"
        elif line.startswith("DEV="):
            info["device"] = line[4:].strip()
        elif line.startswith("XF="):
            info["xformers"] = line[3:].strip()
    return info


def port_open(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def first_free_port(start, end):
    port = start
    while port <= end and port_open(port):
        port += 1
    return port if port <= end else None


def gate_script():
    """登录门禁脚本：打包后在 _MEIPASS 里，源码运行时在 launcher_src 目录。"""
    candidates = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(os.path.join(bundle, "login_gate.py"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "login_gate.py"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def start_gate(python, host, public_port, backend_port):
    """用 venv 的 python 拉起登录门禁，成功返回 Popen 对象，失败返回 None。"""
    script = gate_script()
    if not script:
        print("[错误] 未找到 login_gate.py，登录门禁无法启动，已中止（避免无密码直接暴露服务）。")
        return None
    proc = subprocess.Popen(
        [python, script, str(public_port), str(backend_port), ACCOUNT_USER, ACCOUNT_PASS, host],
        creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if proc.poll() is not None:
            print("[错误] 登录门禁启动失败，端口 " + str(public_port) + " 可能被占用。")
            return None
        if port_open(public_port):
            return proc
        time.sleep(0.1)
    print("[警告] 登录门禁启动超时，继续启动 ComfyUI。")
    return proc


def open_browser(port):
    time.sleep(1.0)
    try:
        webbrowser.open("http://127.0.0.1:" + str(port) + "/")
    except Exception:
        pass


def parse_option(args, name):
    """读取 --name value 或 --name=value，返回字符串或 None。"""
    if name in args:
        idx = args.index(name)
        if idx + 1 < len(args):
            return args[idx + 1]
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def xformers_op_ok(python):
    """ComfyUI 要求 memory_efficient_attention 可用且 xformers 带 C++ 扩展才算生效。"""
    code = ("import xformers, xformers.ops as x;"
            "ok = getattr(x,'memory_efficient_attention',None) and getattr(xformers,'_has_cpp_library',True);"
            "print('ok' if ok else 'bad')")
    try:
        out = subprocess.run([python, "-c", code], capture_output=True, text=True,
                             timeout=180, creationflags=CREATE_NO_WINDOW)
        return out.stdout.strip() == "ok"
    except Exception:
        return False


def cuda_tag():
    """驱动 >= 580 用 cu130（ComfyUI 的优化算子要求），否则回退 cu128。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW,
        )
        major = int(out.stdout.strip().splitlines()[0].split(".")[0])
        return "cu130" if major >= 580 else "cu128"
    except Exception:
        return "cu128"


def pip_install(python, packages, extra=None):
    """依次尝试各 PyPI 镜像，任一成功即返回 True。"""
    for mirror in PYPI_MIRRORS:
        cmd = [python, "-m", "pip", "install"] + packages + (extra or []) + [
            "-i", mirror, "--timeout", "20", "--retries", "2"]
        if subprocess.call(cmd) == 0:
            return True
        print("    当前源不可用，切换下一个: " + mirror)
    return False


def venv_python(venv_dir):
    if IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def install_env(app_dir):
    venv_dir = os.path.join(app_dir, "venv")
    if not os.path.isdir(venv_dir):
        sys_py = shutil.which("python3") or shutil.which("python") or shutil.which("py")
        if not sys_py:
            print("[错误] 未检测到 Python，请先安装 Python 3.11 并加入 PATH。")
            return 1
        print("==> 创建虚拟环境: " + venv_dir)
        if subprocess.call([sys_py, "-m", "venv", venv_dir]) != 0:
            print("[错误] 虚拟环境创建失败。")
            return 1
    python = venv_python(venv_dir)

    print("==> 升级 pip / wheel")
    subprocess.call([python, "-m", "pip", "install", "--upgrade", "pip", "wheel"])

    tag = cuda_tag()
    print("==> 安装 PyTorch (" + tag + "，支持 RTX 20 系 sm75，优先阿里云镜像)")
    torch_ok = subprocess.call([python, "-m", "pip", "install", "torch", "torchvision", "torchaudio",
                                "--no-index", "--find-links", TORCH_MIRROR_TPL.format(tag=tag),
                                "--no-deps"]) == 0
    if not torch_ok:
        print("    镜像安装失败，回退官方源")
        torch_ok = subprocess.call([python, "-m", "pip", "install", "torch", "torchvision", "torchaudio",
                                    "--index-url", TORCH_OFFICIAL_TPL.format(tag=tag)]) == 0
    if not torch_ok:
        print("[错误] PyTorch 安装失败，请检查网络后重试。")
        return 1
    # --no-deps 安装时需补齐 torch 运行期依赖
    if not pip_install(python, ["filelock", "typing-extensions", "sympy",
                                "networkx", "jinja2", "fsspec"]):
        print("[错误] torch 运行依赖安装失败，请检查网络后重试。")
        return 1
    torch_before = subprocess.run([python, "-c", "import torch;print(torch.__version__)"],
                                  capture_output=True, text=True).stdout.strip()
    print("    已安装 torch " + torch_before + " (--no-deps 安装 xformers，卸载时不影响 torch)")

    print("==> 安装 xformers（可选，加速 attention）")
    if pip_install(python, ["xformers"], ["--no-deps"]):
        xf_ok = xformers_op_ok(python)
        if not xf_ok:
            # 新版 xformers 依赖 mslk 才能真正启用 memory_efficient_attention
            print("    xformers 缺少 mslk，尝试补装")
            pip_install(python, ["mslk", "--no-deps"], ["--index-url", TORCH_OFFICIAL_TPL.format(tag=tag)])
            xf_ok = xformers_op_ok(python)
        if not xf_ok:
            print("    xformers 与 torch 不匹配，已卸载回退（将使用 SDPA）")
            subprocess.call([python, "-m", "pip", "uninstall", "-y", "xformers", "mslk"], stdout=subprocess.DEVNULL)
    else:
        print("    跳过 xformers（不影响运行，将使用 PyTorch SDPA）")

    print("==> 安装 ComfyUI 依赖")
    if not pip_install(python, ["-r", os.path.join(app_dir, "requirements.txt")]):
        print("[警告] ComfyUI 依赖安装不完整，启动时可能提示缺少模块。")

    info = probe_env(python)
    print("==> 环境校验")
    print("    torch: " + str(info["torch"]))
    print("    CUDA 可用: " + str(info["cuda"]))
    print("    GPU: " + (info["device"] or "无"))
    if not info["cuda"]:
        print("[警告] CUDA 不可用，请检查 NVIDIA 驱动是否为最新版。")
    print("")
    print("安装完成。Windows 双击 ComfyUI启动器.exe；Linux 运行 python launcher_src/launcher.py --host 0.0.0.0")
    return 0


def main():
    setup_console()
    args = sys.argv[1:]
    base = base_dir()
    app_dir = find_app_dir(base)
    if not app_dir:
        print("[错误] 在 " + base + " 下未找到 main.py，请把 exe 放在 ComfyUI 仓库根目录。")
        return 1

    if "--install-env" in args:
        return install_env(app_dir)

    python = find_python(app_dir)
    if not python:
        print("[错误] 未找到 Python，请先运行 安装环境.bat 或本程序 --install-env。")
        return 1

    info = probe_env(python)
    if not info["torch"]:
        print("[错误] venv 中缺少 PyTorch，启动已中止。")
        if os.path.isdir(os.path.join(app_dir, "venv")):
            print("       若环境正在安装，请等安装完成后再启动；否则运行 ComfyUI启动器.exe --install-env")
        else:
            print("       请先运行 ComfyUI启动器.exe --install-env 安装环境。")
        return 1
    if not info["cuda"]:
        print("[警告] PyTorch 检测不到 CUDA，将回退到 CPU 推理（极慢），请检查 NVIDIA 驱动。")

    vram, vram_used = gpu_memory()
    ram = total_ram_gb()

    dynamic = False
    if info["torch"]:
        try:
            dynamic = tuple(int(x) for x in info["torch"].split(".")[:2]) >= (2, 8)
        except ValueError:
            dynamic = False

    if vram >= 7500:
        new_h, old_h = 0.5, 0.9
    elif vram >= 5500:
        new_h, old_h = 0.6, 0.7
    elif vram > 0:
        new_h, old_h = 0.4, 0.5
    else:
        new_h, old_h = 0.6, 0.7

    launch = ["--vram-headroom", str(new_h)] if dynamic else ["--reserve-vram", str(old_h)]
    # 本地低显存场景不需要云端付费节点，禁用后前端不会再出现“登录以运行”提示
    if "--disable-partner-nodes" not in args:
        launch.append("--disable-partner-nodes")
    # 启用 ComfyUI-Manager（pip 安装的 comfyui-manager），用于补装缺失节点
    if "--enable-manager" not in args and "--disable-manager-ui" not in args:
        launch.append("--enable-manager")

    has_text_enc = any("text-enc" in a for a in args)
    fp8_te = bool(0 < vram < 7500 and not has_text_enc and not os.environ.get("COMFY_NO_FP8_TE"))
    if fp8_te:
        launch.append("--fp8_e4m3fn-text-enc")

    # 对外端口：用户可用 --port 指定，否则 8188 起顺延
    public_port = None
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            try:
                public_port = int(args[idx + 1])
            except ValueError:
                public_port = None
    else:
        for a in args:
            if a.startswith("--port="):
                try:
                    public_port = int(a.split("=", 1)[1])
                except ValueError:
                    public_port = None
    if public_port is None:
        public_port = 8188
        while public_port < 8200 and port_open(public_port):
            public_port += 1

    # 监听地址：默认仅本机；云服务器部署传 --host 0.0.0.0
    bind_host = parse_option(args, "--host") or "127.0.0.1"
    is_local_host = bind_host in ("127.0.0.1", "localhost", "::1")
    no_browser = "--no-browser" in args or not is_local_host

    # 内部端口给 ComfyUI 本体用，登录门禁监听 public_port 并转发
    backend_port = first_free_port(public_port + 100, public_port + 199)
    if backend_port is None:
        print("[错误] 没有可用的内部端口（" + str(public_port + 100) + "-" + str(public_port + 199) + "）。")
        return 1
    launch += ["--port", str(backend_port)]

    # 端口 / 自动打开浏览器由启动器接管，不再透传给 ComfyUI
    clean_args = []
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "--port":
            skip_next = True
            continue
        if a == "--host":
            skip_next = True
            continue
        if a.startswith("--port=") or a.startswith("--host=") or a in ("--auto-launch", "--no-browser"):
            continue
        clean_args.append(a)

    xf_usable = info["xformers"] not in ("none", "broken")
    attn = ("xformers " + info["xformers"]) if xf_usable else "pytorch SDPA"
    print("==================================================")
    print(" Python     : " + python + " (torch " + info["torch"] + ")")
    print(" GPU        : " + (info["device"] or "未知") + " " + str(vram) + "MB, 当前占用 " + str(vram_used) + "MB")
    print(" 内存       : " + str(ram) + " GB")
    print(" Attention  : " + attn)
    print(" 显存策略   : " + ("--vram-headroom" if dynamic else "--reserve-vram") + " "
          + str(new_h if dynamic else old_h) + " (" + ("DynamicVRAM" if dynamic else "legacy 回退") + ")")
    print(" 文本编码器 : " + ("fp8_e4m3fn（低显存优化，COMFY_NO_FP8_TE=1 可关闭）" if fp8_te else "默认 fp16"))
    print(" 访问地址   : http://" + ("127.0.0.1" if not is_local_host else bind_host)
          + ":" + str(public_port) + ("（监听 " + bind_host + "）" if not is_local_host else ""))
    print(" 登录账号   : " + ACCOUNT_USER + " / " + ACCOUNT_PASS + "（本地内置）")
    print(" 程序目录   : " + app_dir)
    print("==================================================")

    if vram_used > 1200:
        print("提示: 显存已被其他程序占用 " + str(vram_used) + "MB，建议关闭浏览器硬件加速/游戏后再启动。")
    if 0 < vram < 7500:
        print("提示: 6G 级别显卡建议使用 SD1.5 / SDXL-Turbo / Flux GGUF-Q4 等轻量模型。")
    if 0 < ram < 20:
        print("提示: 物理内存不足 20G，建议把虚拟内存调到 16G 以上以减少爆内存。")

    gate = start_gate(python, bind_host, public_port, backend_port)
    if gate is None:
        return 1
    if not no_browser:
        threading.Thread(target=open_browser, args=(public_port,), daemon=True).start()

    try:
        return subprocess.call([python, "main.py"] + launch + clean_args, cwd=app_dir)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            gate.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    exit_code = 0
    try:
        exit_code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        exit_code = 1
    # 失败或安装完成后停留，避免双击运行时窗口一闪而过
    interactive = bool(sys.stdin and sys.stdin.isatty())
    need_pause = exit_code not in (0, None) or "--install-env" in sys.argv[1:]
    if interactive and need_pause:
        try:
            input("\n按回车键退出...")
        except Exception:
            pass
    sys.exit(exit_code or 0)
