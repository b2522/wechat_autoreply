# -*- coding: utf-8 -*-
"""
微信PC端 - 群消息自动回复机器人（图像 + OCR + 键鼠自动化）

适用场景：
  微信 4.x（Qt 自绘界面）不暴露消息列表控件，传统 UIAutomation 读不到消息文本，
  因此采用"截图 + OCR"方案：截取聊天区域 -> OCR 识别消息 -> 匹配关键词 -> 自动回复。

用法：
  python wechat_autoreply.py --test-ocr  仅 OCR 识别并打印，不回复（测试用）
  python wechat_autoreply.py --once      检测一轮就退出（测试用）
  python wechat_autoreply.py             正常模式：持续监听并自动回复

依赖：
  pip install uiautomation pillow rapidocr_onnxruntime
"""

import json
import os
import random
import re
import sys
import time
import ctypes
from collections import Counter
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ⚠️ 配置的唯一来源是 config.json（与本脚本同目录）。
#    CONFIG_TEMPLATE 仅用于在「config.json 不存在时」自动生成一份带中文说明的模板文件，
#    它的值不参与任何运行逻辑；运行所需的一切配置都从 config.json 读取，py 文件里不保留业务配置。
CONFIG_TEMPLATE = {
    "_说明": "本文件是唯一配置源。group_name：要监控的微信群名（需与微信窗口标题一致，含此串即可）；keywords：触发关键词列表，消息包含任一即回复；reply：自动回复内容；poll_interval：扫描间隔秒；reply_delay_min/max：检测到后随机延迟的秒数范围；reply_interval_minutes：该分钟数内全局最多发 1 条；scroll_to_bottom_interval：每隔多少秒滚到底部（0=关闭）；max_empty_ocr_recover：连续多少次 OCR 为空就重定位窗口；manual_open_only：true=只用手动打开的窗口不搜索；verbose：true=每轮输出可见关键词数。修改后保存并重启动脚本生效。",
    "group_name": "在此填写要监控的群名",
    "keywords": ["关键词1", "关键词2"],
    "reply": "在此填写自动回复内容",
    "poll_interval": 5,
    "reply_delay_min": 60,
    "reply_delay_max": 120,
    "reply_interval_minutes": 5,
    "scroll_to_bottom_interval": 60,
    "max_empty_ocr_recover": 3,
    "manual_open_only": True,
    "verbose": False
}

# 可选键缺失时的保守兜底（仅防崩溃用，正常 config.json 已包含这些键，不会触发）。
# 业务键（group_name/keywords/reply）为必需，缺失会直接报错退出。
_FALLBACK = {
    "poll_interval": 5,
    "reply_delay_min": 60,
    "reply_delay_max": 120,
    "reply_interval_minutes": 5,
    "scroll_to_bottom_interval": 60,
    "max_empty_ocr_recover": 3,
    "manual_open_only": True,
    "verbose": False,
}
_REQUIRED = ("group_name", "keywords", "reply")


def load_config():
    """config.json 是唯一配置源，py 文件里不保留任何业务配置值。

    - 若 config.json 不存在：用 CONFIG_TEMPLATE 生成一份带中文说明的模板，并退出（请用户填写后重跑）。
    - 若 config.json 存在：读取它（丢弃 _ 开头的注释字段）作为唯一配置。
        * 缺少必需键（group_name/keywords/reply）-> 直接报错退出。
        * 缺少可选键 -> 用 _FALLBACK 兜底（仅防崩溃，正常不会触发，并给出警告）。
    """
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(CONFIG_TEMPLATE, f, ensure_ascii=False, indent=2)
            print(f"[提示] 已生成配置模板：{CONFIG_PATH}")
            print(f"        请填写 group_name / keywords / reply 后重新运行脚本。")
        except Exception as e:
            print(f"[错误] 无法创建 config.json：{e}")
        sys.exit(0)

    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            user_cfg = json.load(f)
    except Exception as e:
        print(f"[错误] 读取 config.json 失败：{e}")
        print(f"        请检查 {CONFIG_PATH} 是否为合法 JSON，或删除它让脚本重新生成模板。")
        sys.exit(1)

    cfg = {k: v for k, v in user_cfg.items() if not k.startswith('_')}

    missing = [k for k in _REQUIRED if k not in cfg]
    if missing:
        print(f"[错误] config.json 缺少必需配置项：{missing}")
        print(f"        请补全后重启脚本；或删除 {CONFIG_PATH} 让其重新生成模板。")
        sys.exit(1)

    # 可选键缺失则用保守兜底（仅防崩溃，正常不触发）；并提示用户
    for k, v in _FALLBACK.items():
        if k not in cfg:
            cfg[k] = v
            print(f"[警告] config.json 缺少可选键 {k!r}，使用内置默认 {v!r}")

    return cfg


CONFIG = load_config()


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    try:
        log_path = os.path.join(SCRIPT_DIR, CONFIG.get('log_file', 'wechat_autoreply.log'))
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


# ---------------------------------------------------------------- 依赖加载

try:
    import uiautomation as auto
except ImportError:
    print("[错误] 缺少 uiautomation，请运行: pip install uiautomation")
    sys.exit(1)

try:
    from PIL import ImageGrab, Image
except ImportError:
    print("[错误] 缺少 Pillow，请运行: pip install pillow")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("[错误] 缺少 numpy，请运行: pip install numpy")
    sys.exit(1)

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:
    print("[错误] 缺少 rapidocr_onnxruntime，请运行: pip install rapidocr_onnxruntime")
    sys.exit(1)


# ---------------------------------------------------------------- 微信窗口定位

def _get_window_rect(win):
    try:
        return win.BoundingRectangle
    except Exception:
        return None


def iter_top_windows():
    """枚举所有顶层窗口（用于模糊匹配群名、自检）"""
    try:
        root = auto.GetRootControl()
        for w in root.GetChildren():
            yield w
    except Exception:
        pass


def find_target_window(group_name):
    """定位目标窗口：先精确匹配群名，再模糊匹配（标题含群名即可），
    最后回退到主微信窗口。手动模式下找不到独立群窗也能拿到主窗口。"""
    # 1) 精确匹配群名（独立浮窗常见情况）
    win = auto.WindowControl(searchDepth=1, Name=group_name)
    if win.Exists(2, 0.3):
        return win

    # 2) 模糊匹配：遍历顶层窗口，标题「包含」群名即可（容错空格/符号差异）
    partial = None
    for w in iter_top_windows():
        try:
            name = w.Name or ''
        except Exception:
            continue
        if not name:
            continue
        if group_name in name:
            # 优先返回非主窗口（即真正独立的群/联系人窗口）
            if name not in ('微信', 'WeChat', 'Weixin'):
                return w
            partial = w
    if partial is not None:
        return partial

    # 3) 主微信窗口标题
    for name in ['微信', 'WeChat', 'Weixin']:
        win = auto.WindowControl(searchDepth=1, Name=name)
        if win.Exists(2, 0.3):
            return win

    # 4) 微信通用 Qt 类名兜底
    win = auto.WindowControl(searchDepth=1, ClassName='Qt51514QWindowIcon')
    if win.Exists(2, 0.3):
        return win

    return None


def ensure_active(win):
    """还原并激活窗口"""
    try:
        if not getattr(win, 'IsVisible', True):
            win.Show()
            time.sleep(0.3)
        try:
            if getattr(win, 'IsMinimize', False):
                win.Show()
                time.sleep(0.3)
        except Exception:
            pass
        win.SetActive()
        time.sleep(0.4)
    except Exception:
        pass


# ---------------------------------------------------------------- 窗口健康检查

def _hwnd(win):
    try:
        return win.NativeWindowHandle
    except Exception:
        return 0


def is_window_minimized(win):
    """判断窗口是否被最小化（最小化后内容不渲染，截图会黑屏）"""
    hwnd = _hwnd(win)
    if not hwnd:
        return False
    try:
        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [('length', ctypes.c_uint),
                        ('flags', ctypes.c_uint),
                        ('showCmd', ctypes.c_uint),
                        ('ptMinPosition', ctypes.c_int * 2),
                        ('ptMaxPosition', ctypes.c_int * 2),
                        ('rcNormalPosition', ctypes.c_int * 4)]
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(wp)
        if ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return wp.showCmd in (2, 7)  # SW_MINIMIZE / SW_SHOWMINNOACTIVE
    except Exception:
        pass
    return False


def restore_window(win):
    """恢复最小化窗口（SW_RESTORE=9 会抢焦点；SW_SHOWNOACTIVATE=4 仅恢复不抢焦点）"""
    hwnd = _hwnd(win)
    try:
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE：恢复但不抢焦点
        else:
            win.Show()
        time.sleep(0.5)
    except Exception:
        try:
            win.Show()
        except Exception:
            pass
        time.sleep(0.5)


def scroll_to_bottom(win):
    """把聊天滚到底部，确保最新消息在可视区；抢焦点后归还前台窗口，降低干扰"""
    try:
        prev = ctypes.windll.user32.GetForegroundWindow()
        ensure_active(win)
        auto.SendKeys('{End}')
        time.sleep(0.4)
        if prev:
            try:
                ctypes.windll.user32.SetForegroundWindow(prev)
            except Exception:
                pass
        time.sleep(0.2)
    except Exception as e:
        log(f"[滚动到底部异常] {e}")


def is_chat_window(win):
    """判断是否为已打开的独立聊天窗口。
    微信 4.x 中：主窗口标题固定为"微信"；独立聊天窗口标题为群名/联系人名。
    不再用宽度判断，避免大屏/窗口拉宽后误判。
    """
    name = win.Name or ''
    if name == '微信':
        return False  # 主窗口
    rect = _get_window_rect(win)
    if rect is None:
        return False
    return (rect.right - rect.left) > 100 and (rect.bottom - rect.top) > 100


def open_group_chat(group_name, max_retry=2):
    """在主微信窗口中通过 Ctrl+F 搜索并打开目标群聊，返回群聊窗口"""
    for attempt in range(max_retry):
        main_win = auto.WindowControl(searchDepth=1, Name='微信')
        if not main_win.Exists(2, 0.3):
            main_win = auto.WindowControl(searchDepth=1, ClassName='Qt51514QWindowIcon')
        if not main_win.Exists(2, 0.3):
            log("[错误] 找不到主微信窗口")
            return None

        ensure_active(main_win)
        log(f"尝试通过搜索打开群（第 {attempt + 1} 次）: {group_name}")

        try:
            auto.SendKeys('{Ctrl}f')
            time.sleep(0.8)
            auto.SendKeys('{Ctrl}a')
            time.sleep(0.1)
            auto.SendKeys(group_name)
            time.sleep(1.5)
            auto.SendKeys('{Down}')
            time.sleep(0.2)
            auto.SendKeys('{Enter}')
            time.sleep(2.0)
        except Exception as e:
            log(f"[搜索过程异常] {e}")
            continue

        # 等待独立聊天窗口出现
        for _ in range(5):
            chat_win = auto.WindowControl(searchDepth=1, Name=group_name)
            if chat_win.Exists(2, 0.3):
                log("已打开目标群聊窗口")
                return chat_win
            time.sleep(0.5)

    log("[失败] 未能通过搜索打开目标群聊，请手动打开该群聊窗口后再运行脚本")
    return None


# ---------------------------------------------------------------- 截图 + OCR

def get_chat_region(win):
    """根据窗口矩形计算聊天区域（排除左侧会话栏、顶部标题、底部输入框）"""
    rect = _get_window_rect(win)
    if rect is None:
        return None

    x1, y1, x2, y2 = rect.left, rect.top, rect.right, rect.bottom
    # 窗口尺寸异常（如 (0,0,0,0) 的未渲染状态）直接返回 None，交由上层重新定位
    if (x2 - x1) < 100 or (y2 - y1) < 100:
        return None
    width = x2 - x1

    # 聊天窗口较窄：没有左侧会话栏，顶部约 80px，底部约 180px（含输入框+发送按钮+间距）
    if width < 1000:
        msg_x1 = x1 + 2
        msg_y1 = y1 + 80
        msg_x2 = x2 - 2
        msg_y2 = y2 - 120
    else:
        # 主窗口：右侧是聊天区，左侧是约 65px 的会话栏
        msg_x1 = x1 + 65
        msg_y1 = y1 + 80
        msg_x2 = x2 - 10
        msg_y2 = y2 - 75

    # 裁剪到屏幕可见区域
    import ctypes
    screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    screen_h = ctypes.windll.user32.GetSystemMetrics(1)
    msg_x1 = max(0, msg_x1)
    msg_y1 = max(0, msg_y1)
    msg_x2 = min(screen_w, msg_x2)
    msg_y2 = min(screen_h, msg_y2)

    if msg_x2 <= msg_x1 or msg_y2 <= msg_y1:
        return None
    return msg_x1, msg_y1, msg_x2, msg_y2


def _printwindow_capture(hwnd, rect):
    """用 PrintWindow 截取整个窗口（即使窗口在后台/被遮挡也能拿到真实内容，且不抢焦点）"""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetWindowDC.argtypes = [ctypes.wintypes.HWND]
    user32.GetWindowDC.restype = ctypes.wintypes.HDC
    user32.ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC, ctypes.wintypes.DWORD]
    user32.PrintWindow.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [ctypes.wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = ctypes.wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [ctypes.wintypes.HDC, ctypes.wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = ctypes.wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = [ctypes.wintypes.HDC, ctypes.wintypes.HBITMAP,
                                ctypes.wintypes.UINT, ctypes.wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = ctypes.c_int
    gdi32.DeleteDC.argtypes = [ctypes.wintypes.HDC]
    gdi32.DeleteDC.restype = ctypes.c_int

    left, top, right, bottom = [int(v) for v in rect]
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    gdi32.SelectObject(mem_dc, bmp)
    try:
        ret = user32.PrintWindow(hwnd, mem_dc, 2)  # PW_RENDERFULLCONTENT
        if ret == 0:
            ret = user32.PrintWindow(hwnd, mem_dc, 0)
        if ret == 0:
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                        ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                        ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                        ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32),
                        ('biYPelsPerMeter', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
                        ('biClrImportant', ctypes.c_uint32)]

        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth = width
        bih.biHeight = -height
        bih.biPlanes = 1
        bih.biBitCount = 32
        bih.biCompression = 0
        buf = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(bih), 0)
        img = Image.frombuffer('RGBA', (width, height), buf, 'raw', 'BGRA', 0, 1)
        return img.convert('RGB')
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def capture_message_area(win):
    """截取聊天区域：优先 PrintWindow（后台窗口也能截到真实内容，不抢焦点），失败回退 BitBlt"""
    region = get_chat_region(win)
    if region is None:
        return None
    x1, y1, x2, y2 = region

    hwnd = _hwnd(win)
    if hwnd:
        rect = _get_window_rect(win)
        if rect is not None:
            full = _printwindow_capture(hwnd, (rect.left, rect.top, rect.right, rect.bottom))
            if full is not None:
                crop = full.crop((x1 - rect.left, y1 - rect.top,
                                  x2 - rect.left, y2 - rect.top))
                return crop

    # 回退：激活窗口后用 BitBlt 截图
    try:
        ensure_active(win)
        time.sleep(0.3)
        return ImageGrab.grab(bbox=(x1, y1, x2, y2))
    except Exception as e:
        log(f"[截图失败] {e}")
        return None


def ocr_text(engine, img):
    """对图片 OCR，返回按 y 坐标排序的文本行列表 [(text, y), ...]"""
    if img is None:
        return []
    try:
        arr = np.array(img)
        result, _ = engine(arr)
        if not result:
            return []
        lines = []
        for item in result:
            box = item[0]
            text = item[1] if len(item) > 1 else ''
            ys = [p[1] for p in box]
            avg_y = sum(ys) / len(ys)
            lines.append((text, avg_y))
        lines.sort(key=lambda x: x[1])
        return lines
    except Exception as e:
        log(f"[OCR 异常] {e}")
        return []


def filter_noise(text):
    """过滤时间戳、系统提示等干扰文本"""
    text = text.strip()
    if re.match(r'^\d{1,2}:\d{2}$', text):
        return True
    noise_patterns = ['撤回了一条消息', '条新消息', '由微信提供', '由元宝提供',
                      '消息自动回复功能', '知乎小程序', '国投量化']
    if any(p in text for p in noise_patterns):
        return True
    return False


# ---------------------------------------------------------------- 自动回复

def is_trigger(text):
    keywords = CONFIG.get('keywords') or []
    if not keywords:
        return False
    text = text.strip()
    if not text:
        return False
    return any(k in text for k in keywords)


def send_reply(win, reply):
    """点击输入框，粘贴并发送回复"""
    rect = _get_window_rect(win)
    if rect is None:
        log("[发送失败] 无法获取窗口矩形")
        return False

    region = get_chat_region(win)
    if region is None:
        log("[发送失败] 无法计算输入框区域")
        return False

    # 输入框在窗口底部、聊天区域下方，取聊天区域中心偏下位置
    input_x = (region[0] + region[2]) // 2
    input_y = rect.bottom - 45

    try:
        ensure_active(win)
        auto.Click(input_x, input_y)
        time.sleep(0.3)

        auto.SendKeys('{Ctrl}a', waitTime=0.05)
        time.sleep(0.1)
        auto.SetClipboardText(reply)
        auto.SendKeys('{Ctrl}v', waitTime=0.05)
        time.sleep(0.3)
        auto.SendKeys('{Enter}', waitTime=0.05)
        time.sleep(0.3)
        return True
    except Exception as e:
        log(f"[发送失败] {e}")
        return False


# ---------------------------------------------------------------- 主逻辑

def resolve_chat_window(group_name):
    """定位到要监听的群聊窗口。
    若 config['manual_open_only'] 为 true，只使用你手动打开/前置的窗口，不会主动搜索或打开群：
      - 优先找「标题包含群名」的独立聊天窗口；
      - 找不到独立群窗时，回退到主微信窗口（前提是你已把该群停在主窗口右侧并显示在最前）。
    否则，会尝试通过 Ctrl+F 搜索并打开目标群。
    """
    manual_only = CONFIG.get('manual_open_only', False)

    win = find_target_window(group_name)
    if win is None:
        if manual_only:
            log(f"[错误] manual_open_only=true，但未找到任何微信窗口。请先登录微信PC端。")
        else:
            log("[错误] 找不到任何微信窗口，请先登录微信PC端")
        return None

    name = win.Name or ''
    rect = _get_window_rect(win)
    valid = rect is not None and (rect.right - rect.left) > 100 and (rect.bottom - rect.top) > 100

    # 情况 A：独立群聊窗口（标题含群名，且不是主窗口）
    if is_chat_window(win) and group_name in name and valid:
        ensure_active(win)
        log(f"已定位到目标群聊窗口: {name}")
        return win

    # 情况 B：主微信窗口（标题为 微信/WeChat/Weixin）
    if name in ('微信', 'WeChat', 'Weixin') and valid:
        if manual_only:
            log(f"[提示] 未找到独立群聊浮窗，改用主微信窗口监控。")
            log(f"        请确保主窗口右侧当前显示的就是「{group_name}」群，并把微信窗口保持在最前。")
        else:
            log(f"[提示] 使用主微信窗口，尝试搜索并打开「{group_name}」...")
            chat_win = open_group_chat(group_name)
            if chat_win:
                return chat_win
        ensure_active(win)
        return win

    # 情况 C：找到了窗口但既不是独立群窗也不是主窗口（基本不会发生）
    if manual_only:
        log(f"[错误] manual_open_only=true，找到的窗口「{name}」不是目标群聊窗口。")
        log(f"        请双击该群把它以独立浮窗打开并保持在最前，或确保主窗口右侧显示该群，然后重启脚本。")
    return None


def run_monitor(test_ocr=False, once=False):
    group_name = CONFIG['group_name']
    reply = CONFIG['reply']
    poll = CONFIG.get('poll_interval', 3)
    delay_min = CONFIG.get('reply_delay_min', 60)
    delay_max = CONFIG.get('reply_delay_max', 120)
    interval = CONFIG.get('reply_interval_minutes', 15) * 60  # 秒
    scroll_int = CONFIG.get('scroll_to_bottom_interval', 60)
    max_empty = CONFIG.get('max_empty_ocr_recover', 3)
    keywords = CONFIG.get('keywords', [])

    log(f"===== 启动 {'OCR测试' if test_ocr else '监听'} =====")
    log(f"群: {group_name} | 关键词: {CONFIG.get('keywords')} | 回复: {reply}")
    log(f"策略: 检测后随机延迟 {int(delay_min)}-{int(delay_max)}s 发送；每 {int(interval // 60)} 分钟最多回复 1 条")
    log(f"健壮性: 每 {int(scroll_int)}s 滚到底部；连续 {max_empty} 次 OCR 为空则重定位窗口")

    win = resolve_chat_window(group_name)
    if win is None:
        return

    ensure_active(win)
    engine = RapidOCR()

    if test_ocr:
        log("进入 OCR 测试模式，截取聊天区域并输出识别结果...")
        for i in range(5):
            img = capture_message_area(win)
            lines = ocr_text(engine, img)
            log(f"--- 第 {i + 1} 次扫描，共 {len(lines)} 行 ---")
            for text, y in lines:
                print(f"  y={int(y):4d} | {text}")
            if once:
                break
            time.sleep(poll)
        return

    prev_visible = Counter()  # 上一次扫描时视野内各消息文本的出现次数，用于计数差分判断"新消息"
    last_reply_time = 0      # 上次实际发送回复的时间戳
    scheduled_at = 0         # 计划发送回复的时间戳（0 表示无待发送）
    first_scan = True
    empty_streak = 0
    last_scroll = 0

    while True:
        try:
            now = time.time()

            # 1) 窗口丢失 → 重连
            if not win.Exists(1, 0.2):
                log("[警告] 群聊窗口丢失，重连中...")
                time.sleep(3)
                win = resolve_chat_window(group_name)
                if win is None:
                    time.sleep(5)
                    continue
                first_scan = True
                empty_streak = 0
                continue

            # 2) 最小化 → 自动恢复（不抢焦点，恢复后窗口才会重新渲染）
            if is_window_minimized(win):
                log("[提示] 窗口被最小化，自动恢复...")
                restore_window(win)
                first_scan = True
                empty_streak = 0
                continue

            # 3) 周期性滚到底部，保证新消息落在可视区（即便群很活跃也不会被挤出视野）
            if scroll_int > 0 and now - last_scroll >= scroll_int:
                scroll_to_bottom(win)
                last_scroll = now
                first_scan = True  # 滚动后重建基线，避免旧消息被当成新触发
                empty_streak = 0
                continue

            # 4) 截图 + OCR
            img = capture_message_area(win)
            if img is None:
                empty_streak += 1
                if empty_streak >= max_empty:
                    log("[警告] 聊天区域持续不可读，重新定位窗口...")
                    win = resolve_chat_window(group_name)
                    first_scan = True
                    empty_streak = 0
                    if win is None:
                        time.sleep(5)
                time.sleep(poll)
                continue

            lines = ocr_text(engine, img)
            if not lines:
                empty_streak += 1
                if empty_streak >= max_empty:
                    log("[警告] 连续多次 OCR 为空（窗口可能未渲染），重新定位窗口...")
                    win = resolve_chat_window(group_name)
                    first_scan = True
                    empty_streak = 0
                    if win is None:
                        time.sleep(5)
                time.sleep(poll)
                continue
            empty_streak = 0

            # 5) 扫描【全部可见行】；用"计数差分"判断哪些是本轮新出现的消息。
            #    说明：本群状态短语（如"广电已封堵"）会被群友反复发送，
            #    若用 set 去重，重复文本会被合并成一条 -> 历史里已有就永远判定为"旧消息"而漏触发。
            #    改用 Counter 按出现次数差分：本次比上次多了几条，即视为新来了几条，照常触发。
            current_counts = Counter()
            for text, y in lines:
                raw = text.strip()
                if not raw or filter_noise(raw):
                    continue
                current_counts[raw] += 1

            # 首次扫描只记录基线，不触发回复，避免对历史消息刷屏
            if first_scan:
                prev_visible = current_counts
                log(f"首次扫描完成，已记录 {sum(current_counts.values())} 条可见消息作为基线")
                first_scan = False
                if once:
                    log("--once 模式结束")
                    break
                time.sleep(poll)
                continue

            # verbose：每轮输出当前可见关键词数量，方便排查"有没有看到关键词"
            if CONFIG.get('verbose', False):
                kw_hits = sum(1 for raw in current_counts if is_trigger(raw))
                if kw_hits:
                    log(f"[verbose] 当前可见含关键词消息: {kw_hits} 条")
                else:
                    log(f"[verbose] 当前可见 {sum(current_counts.values())} 条，未识别到关键词")

            # 与上次扫描做计数差分，找出"新出现"的含关键词消息
            for raw, cnt in current_counts.items():
                # 跳过自己刚发出去的回复，避免死循环
                if raw == reply or raw.startswith(reply):
                    continue
                if not is_trigger(raw):
                    continue

                prev_seen = prev_visible.get(raw, 0)
                new_ones = cnt - prev_seen
                if new_ones <= 0:
                    continue  # 本次出现次数未超过上次 -> 非新消息

                # 全局限流：距上次回复不足 interval 则忽略
                if now - last_reply_time < interval:
                    log(f"[跳过] {int(interval // 60)} 分钟内已回复过，忽略触发: {raw}")
                    continue
                # 已有待发送任务，不再重复排期
                if scheduled_at > 0:
                    continue

                delay = random.uniform(delay_min, delay_max)
                scheduled_at = now + delay
                log(f"[计划] 检测到触发「{raw}」（新 {new_ones} 条），将在 {delay:.0f}s 后回复")
                break  # 每轮扫描只排期一次回复

            # 用本次视野更新"上一次可见消息"计数集合（用于下一轮差分）
            prev_visible = current_counts

            # 6) 到点发送（随机延迟后）
            if scheduled_at > 0 and time.time() >= scheduled_at:
                if time.time() - last_reply_time >= interval:
                    if send_reply(win, reply):
                        last_reply_time = time.time()
                        log(f"[已回复] {reply}")
                        scheduled_at = 0
                    else:
                        log("[回复失败，10s 后重试]")
                        scheduled_at = time.time() + 10
                else:
                    scheduled_at = 0

            if once:
                log("--once 模式结束")
                break

            time.sleep(poll)

        except KeyboardInterrupt:
            log("已手动停止")
            break
        except Exception as e:
            log(f"[异常] {e}")
            time.sleep(poll)


# ---------------------------------------------------------------- 入口

def main():
    args = sys.argv[1:]
    test_ocr = '--test-ocr' in args
    once = '--once' in args

    if '--diagnose' in args:
        win = find_target_window(CONFIG['group_name'])
        if win is None:
            log("未找到微信窗口")
            return
        log(f"窗口: Name={win.Name!r} Class={win.ClassName!r} Rect={win.BoundingRectangle}")
        return

    if '--list-windows' in args:
        log("==== 枚举所有顶层窗口（用于核对群窗口标题）====")
        hit = False
        for w in iter_top_windows():
            try:
                name = w.Name or ''
                cls = w.ClassName or ''
            except Exception:
                continue
            if not name and not cls:
                continue
            mark = '  <== 标题含群名' if CONFIG['group_name'] in name else ''
            log(f"  Name={name!r} Class={cls!r}{mark}")
        log(f"配置群名 group_name = {CONFIG['group_name']!r}")
        log("提示：若没有带 '<== 标题含群名' 的窗口，说明微信里该群窗口标题与配置不一致，或群未以独立浮窗/主窗右侧打开。")
        return

    run_monitor(test_ocr=test_ocr, once=once)


if __name__ == '__main__':
    main()
