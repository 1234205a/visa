# -*- coding: utf-8 -*-
"""Automate the Rome consulate appointment flow with Playwright.

The script reproduces the manual steps requested by the user:

1. 打开预约页面并点击“Accéder aux services”。
2. 如果弹出会话恢复对话框则点击“Oui”，直接进入信息确认页面。
3. 若 1 秒内页面无反应，则提示完成 Cloudflare 人机验证，待用户确认后继续。
4. 当进入服务选择页时，将人数和“Demande de visa”服务数量都设置为 2，然后点“Confirmer”。
5. 在信息确认页勾选“J'ai bien lu ...”后点击“Prendre rendez-vous”。
6. 如果日历页仍显示“暂无预约”提示，则刷新回到首页继续循环；否则发出提示音并停止。

需要提前安装 Playwright 及浏览器驱动：

```bash
pip install playwright
playwright install chromium
```
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

from playwright.sync_api import (  # type: ignore[import-untyped]
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# URL 和页面文本常量
HOME_URL = "https://consulat.gouv.fr/ambassade-de-france-a-rome/rendez-vous"
SESSION_MODAL_TEXT = "Une session valide contenant les informations de votre formulaire"  # 会话恢复对话框文本
INFO_STEP_HEADING = "Informations importantes"  # 信息确认页面标题
SERVICES_HEADING = "Pour combien de personnes souhaitez-vous prendre rendez-vous ?"  # 服务选择页面标题
CONSENT_TEXT = "J'ai bien lu l'ensemble des informations disponibles sur le site du Consulat"  # 确认勾选框文本
UNAVAILABLE_TEXT = "Aucun rendez-vous n'est disponible pour le moment"  # 无可用预约提示
SUCCESS_SOUND_DELAY = 0.3  # 成功提示音之间的延迟（秒）

# Telegram 配置（不要把真实token/chat_id写死在这里，用 --telegram-bot-token / --telegram-chat-id 传入）
TELEGRAM_BOT_TOKEN: Optional[str] = None
TELEGRAM_CHAT_ID: Optional[str] = None


@dataclass
class Config:
    headless: bool
    poll_delay: float
    slow_mo: Optional[int]
    user_data_dir: Path
    browser_args: Tuple[str, ...]
    step_delay_ms: int


def beep(times: int = 1, delay: float = SUCCESS_SOUND_DELAY) -> None:
    """Emit an audible alert in the terminal."""

    for _ in range(times):
        # ASCII bell character works on most terminals.
        print("\a", end="", flush=True)
        time.sleep(delay)


def show_alert(title: str, message: str) -> None:
    """显示 Windows 弹窗提示。"""
    try:
        # MB_OK = 0, MB_ICONINFORMATION = 0x40, MB_TOPMOST = 0x40000
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x40000)
    except Exception as exc:
        print(f"[!] 无法显示弹窗：{exc}")


def send_telegram_message(message: str, urgent: bool = False) -> bool:
    """发送 Telegram 消息通知。

    参数:
        message: 要发送的消息内容
        urgent: 是否紧急消息（会禁用通知声音的静音）

    返回:
        True: 发送成功
        False: 发送失败或未配置
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    if not REQUESTS_AVAILABLE:
        print("[!] 未安装 requests 库，无法发送 Telegram 通知")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_notification": not urgent  # 紧急消息会发出声音
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            return True
        else:
            print(f"[!] Telegram 消息发送失败：{response.status_code} - {response.text}")
            return False
    except Exception as exc:
        print(f"[!] Telegram 消息发送异常：{exc}")
        return False


class ConsulateWatcher:
    def __init__(self, page: Page, config: Config) -> None:
        self.page = page
        self.config = config
        self._step_delay_ms = max(0, config.step_delay_ms)

    def run(self) -> None:
        """Main monitoring loop."""

        attempt = 0
        last_long_rest = 0
        while True:
            attempt += 1
            
            # 每 10-15 次检测后休息 30-60 秒
            if attempt - last_long_rest >= random.randint(10, 15):
                rest_duration = random.uniform(30, 60)
                print(f"\n💤 模拟休息 {rest_duration:.0f} 秒...")
                time.sleep(rest_duration)
                print("继续监控...\n")
                last_long_rest = attempt
            print(f"\n=== 尝试第 {attempt} 次 ===")
            self.navigate_home()

            # 计算随机延迟（6-9秒随机，平均7.5秒）
            random_delay = random.uniform(6.0, 9.0)

            try:
                if self.process_flow():
                    beep(times=5)
                    logging.info("🎉🎉🎉 发现可用预约时段！🎉🎉🎉")
                    print("\n" + "="*60)
                    print("🎉 检测到可能有可用的预约时段！")
                    print("="*60)
                    print("浏览器窗口将保持开启，请手动完成预约。")
                    print("完成后请按 Ctrl+C 或直接关闭此窗口来停止程序。")
                    print("="*60 + "\n")

                    # 发送 Telegram 通知（紧急）
                    send_telegram_message(
                        f"🎉🎉🎉 <b>发现可用预约时段！</b> 🎉🎉🎉\n\n"
                        f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"使馆: 法国驻罗马使馆\n\n"
                        f"⚠️ 请立即在浏览器中手动完成预约！",
                        urgent=True
                    )

                    # 显示弹窗提示
                    show_alert(
                        "🎉 发现可用预约时段！",
                        "检测到可能有可用的预约时段！\n\n请在浏览器窗口中手动完成预约。"
                    )

                    # 保持浏览器窗口开启，无限等待用户操作
                    try:
                        while True:
                            time.sleep(60)  # 每60秒检查一次，保持程序运行
                    except KeyboardInterrupt:
                        print("\n程序已被用户停止。")
                        return

                logging.info(f"第 {attempt} 次检测: 暂无预约")
                print(
                    f"页面仍显示暂无预约信息，将在 {random_delay:.1f} 秒后重新尝试。"
                )
            except PlaywrightTimeoutError as exc:
                beep(times=2)
                print(f"[!] 操作超时：{exc}. 将在 {random_delay:.1f} 秒后重试。")

            time.sleep(random_delay)

    # ----- Step helpers -------------------------------------------------
    def navigate_home(self) -> None:
        print("打开预约首页……")
        self.page.goto(HOME_URL, wait_until="load")
        with contextlib.suppress(PlaywrightTimeoutError):
            self.page.wait_for_load_state("networkidle", timeout=5000)

        # 模拟人类行为：随机滚动页面
        try:
            # 随机滚动 1-3 次
            scroll_times = random.randint(1, 3)
            for _ in range(scroll_times):
                scroll_amount = random.randint(150, 400)
                self.page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                self.page.wait_for_timeout(random.randint(500, 1200))
            
            # 30% 概率向上回滚
            if random.random() < 0.3:
                self.page.evaluate(f"window.scrollBy(0, -{random.randint(50, 150)})")
                self.page.wait_for_timeout(random.randint(300, 700))
        except Exception:
            pass

        self._pause()

    def process_flow(self) -> bool:
        """执行预约流程。

        返回:
            True: 检测到可用预约时段
            False: 仍然没有可用预约
        """
        button = self.page.get_by_role("button", name="Accéder aux services")
        print('点击"Accéder aux services"按钮……')
        button.click()

        # 给页面更多时间加载，先等待 2 秒
        self.page.wait_for_timeout(2000)
        
        # 首先检查会话恢复对话框（优先级最高）
        if self.handle_resume_modal():
            print('检测到会话恢复对话框，已点击"Oui"。')
            return self.confirm_information()

        # 检查是否直接进入信息确认页面
        if self.wait_for_information_step(short_timeout=True):
            print("直接进入信息确认页面。")
            return self.confirm_information()

        # 检查是否进入服务选择页面
        if self.wait_for_services_step(short_timeout=True):
            print("进入服务选择页面。")
            self.prepare_service_selection()
            return self.confirm_information(require_manual_consent=True)

        # 如果以上都不是，再次检查会话恢复对话框（可能加载慢）
        if self.handle_resume_modal():
            print('延迟检测到会话恢复对话框，已点击"Oui"。')
            return self.confirm_information()

        # 最后才判断为需要验证
        print("按钮点击后页面无反应，可能触发 Cloudflare 验证。")
        self.await_cloudflare_validation()
        button = self.page.get_by_role("button", name="Accéder aux services")
        print('验证完成后再次点击"Accéder aux services"。')
        button.click()

        # 验证后也要检查会话恢复对话框！
        if self.handle_resume_modal():
            print('检测到会话恢复对话框，已点击"Oui"。')
            return self.confirm_information()

        if self.wait_for_information_step(short_timeout=True):
            print("验证后进入信息确认页面。")
            return self.confirm_information()

        if not self.wait_for_services_step():
            print("未能加载服务选择页面，将重新轮询。")
            return False

        self.prepare_service_selection()
        return self.confirm_information(require_manual_consent=True)

    def handle_resume_modal(self) -> bool:
        """处理会话恢复对话框，如果出现则点击 Oui。

        返回:
            True: 检测到对话框并成功点击
            False: 未检测到对话框
        """
        # 增加等待时间，让对话框有足够时间出现
        self.page.wait_for_timeout(1500)

        # 方法1：通过对话框定位
        modal = (
            self.page.get_by_role("dialog")
            .filter(has_text=re.compile(r"Une session valide", re.I))
            .nth(0)
        )
        try:
            modal.wait_for(state="visible", timeout=4000)  # 增加到4秒
        except PlaywrightTimeoutError:
            # 方法2：直接搜索包含关键文字的元素
            try:
                text_locator = self.page.locator("text=Une session valide contenant les informations")
                text_locator.wait_for(state="visible", timeout=3000)  # 增加到3秒
            except PlaywrightTimeoutError:
                return False

        def _try_click(locator) -> bool:
            if locator.count() == 0:
                return False
            try:
                locator.first.click(timeout=2000)
                # 点击后等待一下，确保对话框关闭
                self.page.wait_for_timeout(800)  # 增加等待时间
                return True
            except Exception:
                return False

        # 尝试多种方式定位"Oui"按钮
        candidates = [
            modal.get_by_role("button", name="Oui", exact=True),
            modal.get_by_role("button", name=re.compile(r"\bOui\b", re.I)),
            modal.locator("button").filter(has_text=re.compile(r"\bOui\b", re.I)),
            self.page.get_by_role("button", name="Oui", exact=True),
            self.page.get_by_role("button", name=re.compile(r"\bOui\b", re.I)),
            self.page.locator("button").filter(has_text=re.compile(r"\bOui\b", re.I)),
        ]

        for candidate in candidates:
            if _try_click(candidate):
                return True

        print('[!] 检测到对话框但无法点击"Oui"按钮')
        raise PlaywrightTimeoutError('未能定位到"Oui"按钮。')

    def wait_for_information_step(self, short_timeout: bool = False) -> bool:
        """等待信息确认页面加载。

        参数:
            short_timeout: 是否使用较短的超时时间（1.5秒 vs 8秒）

        返回:
            True: 页面加载成功
            False: 页面未加载
        """
        timeout = 1500 if short_timeout else 8000
        heading_locator = self.page.get_by_role(
            "heading", name=re.compile(INFO_STEP_HEADING, re.I)
        ).first
        try:
            heading_locator.wait_for(state="visible", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            # 某些情况下只有文本节点出现，退回 text 定位
            text_locator = self.page.locator(f"text={INFO_STEP_HEADING}").first
            try:
                text_locator.wait_for(state="visible", timeout=timeout)
                return True
            except PlaywrightTimeoutError:
                return False

    def await_cloudflare_validation(self) -> None:
        beep(times=3)
        print(
            "[!] 检测到 Cloudflare 验证。等待5秒让验证框加载……"
        )
        # 等待验证框完全加载
        self.page.wait_for_timeout(5000)

        print("[!] 请手动完成页面中的 Cloudflare 人机验证。完成后按 Enter 继续。")

        # 发送 Telegram 通知
        send_telegram_message(
            f"⚠️ <b>需要人工验证</b>\n\n"
            f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"检测到 Cloudflare 人机验证！\n"
            f"请在浏览器窗口中完成验证，然后在控制台按 Enter 继续。",
            urgent=True
        )

        # 显示弹窗提示
        show_alert(
            "⚠️ 需要人工验证",
            "检测到 Cloudflare 人机验证！\n\n请在浏览器窗口中完成验证，\n然后在控制台按 Enter 继续。"
        )
        input("完成验证了吗？按 Enter 继续……")

    def wait_for_services_step(self, short_timeout: bool = False) -> bool:
        timeout = 2500 if short_timeout else 10000
        try:
            self.page.wait_for_selector(f"text={SERVICES_HEADING}", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            return False

    def prepare_service_selection(self) -> None:
        """在服务选择页面设置人数和服务数量。"""
        print('将人数和"Demande de visa"数量设置为 2……')
        if not self._set_people_count(2):
            print("[!] 未能自动调整来访人数，请检查页面结构。")
            raise PlaywrightTimeoutError("未能调整来访人数。")
        if not self._set_service_quantity("Demande de visa", 2):
            print('[!] 未能自动调整"Demande de visa"服务数量，请检查页面结构。')
            raise PlaywrightTimeoutError("未能设置 Demande de visa 数量。")

        confirm = self.page.get_by_role("button", name=re.compile(r"Confirmer", re.I))
        confirm.scroll_into_view_if_needed()
        confirm.wait_for(state="attached", timeout=5000)
        confirm.wait_for(state="visible", timeout=5000)
        self._wait_until_enabled(confirm)
        self.page.wait_for_timeout(500)
        self._click_until_navigation(confirm, target_heading=re.compile(INFO_STEP_HEADING, re.I))
        if not self.wait_for_information_step(short_timeout=False):
            print('[!] 点击"Confirmer"后未进入信息确认页面，可能仍停留在服务页。')
            raise PlaywrightTimeoutError("确认服务后未进入信息确认页面。")

    def confirm_information(self, require_manual_consent: bool = False) -> bool:
        """处理信息确认页面，勾选确认框并点击预约按钮。

        参数:
            require_manual_consent: 是否需要手动勾选确认框

        返回:
            True: 检测到可用预约时段
            False: 仍然没有可用预约
        """
        print("处理信息确认页面……")
        if not self.wait_for_information_step():
            raise PlaywrightTimeoutError("未检测到信息确认页标题。")
        checkbox = self._locate_confirmation_checkbox()
        checkbox.wait_for(state="attached", timeout=5000)
        checkbox.scroll_into_view_if_needed()

        if require_manual_consent or not checkbox.is_checked():
            print("勾选确认框……")
            try:
                checkbox.check(timeout=1500)
                self._dispatch_input_events(checkbox)
            except Exception:
                with contextlib.suppress(Exception):
                    label = self.page.locator("label[for='readInformations']")
                    if label.count():
                        label.first.click()
                        self.page.wait_for_timeout(300)
                if not checkbox.is_checked():
                    self._dispatch_input_events(checkbox, set_checked=True)
            self.page.wait_for_timeout(500)

        button = self.page.get_by_role("button", name="Prendre rendez-vous")
        button.wait_for(state="visible")
        self._wait_until_enabled(button)

        print('点击"Prendre rendez-vous"按钮……')
        # 使用多次尝试点击，确保导航成功
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                button.click(timeout=3000)
                # 等待一下看页面是否开始导航
                self.page.wait_for_timeout(1000)

                # 检查是否还在信息确认页面
                info_heading = self.page.get_by_role("heading", name=re.compile(INFO_STEP_HEADING, re.I))
                if info_heading.count() == 0 or not info_heading.first.is_visible():
                    break

                if attempt < max_attempts:
                    print(f"重试点击按钮（{attempt}/{max_attempts}）……")
            except Exception as exc:
                if attempt == max_attempts:
                    print(f"[!] 点击失败：{exc}")
                    raise

        return self.evaluate_calendar()

    def _click_until_navigation(self, button, target_heading: Optional[re.Pattern[str]] = None) -> None:
        """重复点击按钮直到页面导航成功。"""
        start = time.time()
        attempt = 0
        max_timeout = 12

        while time.time() - start < max_timeout:
            attempt += 1
            # 尝试点击按钮
            try:
                button.click(timeout=2000, force=True)
            except Exception:
                with contextlib.suppress(Exception):
                    button.evaluate("(el) => el.click()")

            self.page.wait_for_timeout(500)

            # 检查是否导航成功
            if target_heading:
                heading = self.page.get_by_role("heading", name=target_heading)
                if heading.count():
                    with contextlib.suppress(PlaywrightTimeoutError):
                        heading.first.wait_for(state="visible", timeout=1000)
                        return

            if self.wait_for_information_step(short_timeout=True):
                return

        raise PlaywrightTimeoutError(f"点击按钮 {max_timeout} 秒后仍未检测到页面跳转。")

    def _wait_until_enabled(self, button, max_attempts: int = 20) -> None:
        """等待按钮变为可用状态。"""
        for attempt in range(1, max_attempts + 1):
            try:
                disabled_attr = button.get_attribute("disabled")
                if button.is_enabled() and not disabled_attr:
                    return
            except Exception as exc:
                if attempt == max_attempts:
                    print(f"[!] 检查按钮状态失败：{exc}")
            self._pause(divisor=2)
        raise PlaywrightTimeoutError("按钮未在预期时间内变为可用。")

    def evaluate_calendar(self) -> bool:
        """评估日历页面，检查是否有可用预约。

        返回:
            True: 未检测到暂无预约提示，可能有可用时段
            False: 显示暂无预约
        """
        print("等待预约日历加载……")

        # 首先确保已离开"Informations importantes"页面
        info_heading = self.page.get_by_role("heading", name=re.compile(INFO_STEP_HEADING, re.I))
        try:
            # 等待标题消失（最多10秒）
            info_heading.first.wait_for(state="hidden", timeout=10000)
        except PlaywrightTimeoutError:
            print("[!] 警告：仍在信息确认页面，可能按钮点击未生效")

        # 等待网络空闲和页面加载
        self.page.wait_for_load_state("load", timeout=15000)
        with contextlib.suppress(PlaywrightTimeoutError):
            self.page.wait_for_load_state("networkidle", timeout=8000)

        # 额外等待，确保日历内容渲染
        self.page.wait_for_timeout(3000)
        self._pause()

        # 检查日历是否真的加载完成（检测 Invalid Date 或加载中状态）
        print("检查日历加载状态……")
        max_wait = 15  # 最多等待15秒
        for i in range(max_wait):
            try:
                page_text = self.page.locator("body").inner_text()
                page_lower = page_text.lower()

                # 如果页面显示 "Invalid Date"，说明还在加载
                if "invalid date" in page_lower:
                    print(f"日历仍在加载中（检测到 Invalid Date），等待... ({i+1}/{max_wait}秒)")
                    self.page.wait_for_timeout(1000)
                    continue

                # 检查是否有日历元素（月份名称应该出现）
                # 常见法语月份：janvier, février, mars, avril, mai, juin, juillet, août, septembre, octobre, novembre, décembre
                months_pattern = r"(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+\d{4}"
                if not re.search(months_pattern, page_lower):
                    print(f"日历月份未显示，继续等待... ({i+1}/{max_wait}秒)")
                    self.page.wait_for_timeout(1000)
                    continue

                # 日历加载完成
                print("✓ 日历加载完成")
                break
            except Exception as exc:
                print(f"[!] 检查加载状态时出错：{exc}")
                self.page.wait_for_timeout(1000)
        else:
            print("[!] 日历加载超时，继续检测（可能仍在加载中）")

        # 获取页面文本（必须成功）
        try:
            page_text = self.page.locator("body").inner_text()
        except Exception as exc:
            print(f"[!] 无法读取页面文本：{exc}")
            print("假设暂无预约。")
            return False

        # 转换为小写用于不区分大小写的匹配
        page_lower = page_text.lower()

        # 如果此时仍然显示 "Invalid Date"，说明加载失败，视为暂无预约
        if "invalid date" in page_lower:
            print("[!] 日历显示 Invalid Date（加载失败或服务器问题），视为暂无预约。")
            return False

        # 检测"暂无预约"的多个关键词组合
        no_appointment_indicators = [
            ("aucun rendez-vous", "disponible"),  # 标准短语
            ("aucun", "rendez-vous", "moment"),     # 变体1
            ("pas de rendez-vous", "disponible"),   # 变体2
        ]

        for keywords in no_appointment_indicators:
            if all(keyword in page_lower for keyword in keywords):
                print("页面显示暂无预约，准备刷新。")
                return False

        # 额外检查：如果有"通知我"按钮文本，也说明暂无预约
        if "être informé lorsqu'un rendez-vous est disponible" in page_lower:
            print("页面显示暂无预约，准备刷新。")
            return False

        # 如果都没检测到，可能真的有预约 - 现在才截图
        print('✓ 未检测到"暂无预约"提示！')
        print('可能出现可预约时段，正在保存截图……')

        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = Path(f"available_slot_{timestamp}.png")
            self.page.screenshot(path=str(screenshot_path))
            print(f"[截图已保存] {screenshot_path}")
        except Exception as exc:
            print(f"[!] 无法保存截图：{exc}")

        return True

    def _pause(self, multiplier: float = 1.0, divisor: float = 1.0) -> None:
        delay = int(self._step_delay_ms * multiplier / divisor)
        if delay <= 0:
            return
        self.page.wait_for_timeout(delay)

    def _dispatch_input_events(self, element, set_checked: Optional[bool] = None) -> None:
        """在元素上触发 input 和 change 事件，可选设置 checked 属性。"""
        with contextlib.suppress(Exception):
            if set_checked is not None:
                element.evaluate(
                    "(el, checked) => {"
                    " el.checked = checked;"
                    " el.dispatchEvent(new Event('input', { bubbles: true }));"
                    " el.dispatchEvent(new Event('change', { bubbles: true }));"
                    "}",
                    set_checked
                )
            else:
                element.evaluate(
                    "(el) => {"
                    " el.dispatchEvent(new Event('input', { bubbles: true }));"
                    " el.dispatchEvent(new Event('change', { bubbles: true }));"
                    "}"
                )

    def _set_people_count(self, target: int) -> bool:
        container = self.page.locator("div.form-inline.service-with-user-count").first
        if not container.count():
            print("[!] 未找到来访人数的旋钮容器。")
            return False
        return self._set_spin_group(container, target)

    def _set_service_quantity(self, service_name: str, target: int) -> bool:
        pattern = re.compile(r"\s*".join(map(re.escape, service_name.split())), re.I)
        candidate_locators = [
            self.page.locator("div.service-applicant").filter(has_text=pattern),
            self.page.locator("div.service-row").filter(has_text=pattern),
            self.page.locator("fieldset").filter(has_text=pattern),
        ]

        for candidate in candidate_locators:
            if candidate.count():
                row = candidate.first
                container = row.locator("div.form-inline.service-with-user-count").first
                if not container.count():
                    container = row
                if self._set_spin_group(container, target):
                    return True

        span_matches = self.page.locator("span").filter(has_text=pattern)
        for idx in range(span_matches.count()):
            span = span_matches.nth(idx)
            container = span.locator(
                "xpath=ancestor::div[contains(@class,'service-applicant') or contains(@class,'service-row')][1]"
            ).first
            if not container.count():
                continue
            inner = container.locator("div.form-inline.service-with-user-count").first
            if inner.count():
                container = inner
            if self._set_spin_group(container, target):
                return True

        print(f'[!] 未找到服务"{service_name}"的行。')
        return False

    def _set_spin_group(self, container, target: int) -> bool:
        """设置旋钮（spinbutton）的值到目标数字。

        参数:
            container: 包含旋钮控件的容器元素
            target: 目标值

        返回:
            True: 成功设置到目标值
            False: 设置失败
        """
        if not container.count():
            return False
        container.wait_for(state="visible", timeout=5000)
        container.scroll_into_view_if_needed()
        output = container.locator('output[role="spinbutton"]').first
        if output.count() == 0:
            output = container.locator("output").first
        if output.count() == 0:
            print("[!] 未找到旋钮对应的 output 元素。")
            return False
        buttons = container.locator("button")
        if buttons.count() == 0:
            print("[!] 旋钮容器中未找到任何按钮。")
            return False

        controls_id = output.get_attribute("id") or output.get_attribute("aria-controls")

        def pick(locator):
            return locator.first if locator.count() else None

        plus_button = None
        minus_button = None

        if controls_id:
            plus_button = pick(container.locator(f'button[aria-controls="{controls_id}"][aria-label*="Plus"]'))
            minus_button = pick(container.locator(f'button[aria-controls="{controls_id}"][aria-label*="Moins"]'))
            if plus_button is None:
                plus_button = pick(
                    container.locator(f'button[aria-controls="{controls_id}"]').filter(has_text=re.compile(r"\+\s*$"))
                )
            if minus_button is None:
                minus_button = pick(
                    container.locator(f'button[aria-controls="{controls_id}"]').filter(has_text=re.compile(r"-|\u2212|\u2013"))
                )

        if plus_button is None:
            plus_button = pick(container.get_by_role("button", name=re.compile("plus", re.I)))
        if minus_button is None:
            minus_button = pick(container.get_by_role("button", name=re.compile("moins|minus", re.I)))

        if plus_button is None:
            plus_button = pick(container.locator("button").filter(has_text=re.compile(r"\+\s*$")))
        if minus_button is None:
            minus_button = pick(container.locator("button").filter(has_text=re.compile(r"-|\u2212|\u2013")))

        if plus_button is None and buttons.count() >= 2:
            plus_button = buttons.last
        if minus_button is None and buttons.count() >= 1:
            minus_button = buttons.first if buttons.count() == 1 else buttons.nth(0)

        if plus_button is None or minus_button is None:
            print("[!] 无法在容器内定位加减按钮。")
            return False

        def current_value() -> Optional[int]:
            try:
                text = output.inner_text().strip()
            except Exception:
                return None
            match = re.search(r"\d+", text)
            return int(match.group(0)) if match else None

        for _ in range(12):
            value = current_value()
            if value == target:
                return True
            if value is None or value < target:
                try:
                    plus_button.click(timeout=1500, force=True)
                except Exception as exc:
                    print(f'[!] 点击加号失败：{exc}')
                    return False
            else:
                try:
                    minus_button.click(timeout=1500, force=True)
                except Exception as exc:
                    print(f'[!] 点击减号失败：{exc}')
                    return False
            self._dispatch_input_events(output)
            self.page.wait_for_timeout(500)
            self._pause(divisor=2)
        final = current_value()
        if final != target:
            print(f"[!] 无法设置到目标值（当前：{final}，目标：{target}）")
        return final == target

    def _locate_confirmation_checkbox(self):
        strategies = [
            ("id", lambda: self.page.locator("input#readInformations")),
            ("name", lambda: self.page.locator("input[name='readInformations']")),
            (
                "type+class",
                lambda: self.page.locator("input[type='checkbox'].custom-control-input"),
            ),
            ("first checkbox", lambda: self.page.locator("input[type='checkbox']").first),
        ]

        for strategy, locator_fn in strategies:
            candidate = locator_fn()
            if candidate.count():
                return candidate.first
        raise PlaywrightTimeoutError("未找到信息确认页的确认复选框。")


def parse_args(argv: list[str]) -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="以无头模式运行浏览器（默认会打开可视窗口，便于手动验证）",
    )
    parser.add_argument(
        "--poll-delay",
        type=float,
        default=7.5,
        help="每次检测之间的基准等待时间（秒），实际会在 6-9 秒随机波动。",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=None,
        help="为调试添加 Playwright slow_mo（毫秒）",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=Path.home() / ".consulate_watcher_profile",
        help="浏览器持久化数据目录，便于保留登录状态/Cloudflare 记录。",
    )
    parser.add_argument(
        "--step-delay-ms",
        type=int,
        default=300,
        help="页面操作之间的基础停顿时间（毫秒），实际会有 ±30%% 的随机波动。",
    )
    parser.add_argument(
        "--telegram-bot-token",
        type=str,
        default=None,
        help="Telegram Bot Token（用于发送通知）",
    )
    parser.add_argument(
        "--telegram-chat-id",
        type=str,
        default=None,
        help="Telegram Chat ID（用于接收通知）",
    )
    args = parser.parse_args(argv)
    # 只使用最基础的反检测参数，避免过于激进
    default_browser_args: Tuple[str, ...] = (
        "--disable-blink-features=AutomationControlled",
    )
    return Config(
        headless=args.headless,
        poll_delay=args.poll_delay,
        slow_mo=args.slow_mo,
        user_data_dir=args.user_data_dir,
        browser_args=default_browser_args,
        step_delay_ms=max(0, args.step_delay_ms),
    )


def main(argv: Optional[list[str]] = None) -> int:
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    # 解析命令行参数
    args_list = argv or sys.argv[1:]

    # 提取Telegram配置
    telegram_token = None
    telegram_chat = None
    for i, arg in enumerate(args_list):
        if arg == "--telegram-bot-token" and i + 1 < len(args_list):
            telegram_token = args_list[i + 1]
        elif arg == "--telegram-chat-id" and i + 1 < len(args_list):
            telegram_chat = args_list[i + 1]

    # 只有在命令行提供了参数时才覆盖内置配置
    if telegram_token:
        TELEGRAM_BOT_TOKEN = telegram_token
    if telegram_chat:
        TELEGRAM_CHAT_ID = telegram_chat

    config = parse_args(args_list)

    # 设置日志记录
    log_file = Path("appointment_monitor.log")
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    logging.info("=" * 50)
    logging.info("脚本启动")
    logging.info(f"检测间隔: {config.poll_delay}秒 (实际: {config.poll_delay*0.8:.1f}-{config.poll_delay*1.2:.1f}秒)")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        logging.info("✓ Telegram 通知已启用")
        # 发送测试消息
        if send_telegram_message(
            f"✅ <b>签证预约监控脚本已启动</b>\n\n"
            f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"检测间隔: {config.poll_delay:.1f}秒\n\n"
            f"脚本正在运行，发现预约时会立即通知您！",
            urgent=False
        ):
            logging.info("✓ Telegram 测试消息发送成功")
        else:
            logging.info("✗ Telegram 消息发送失败，请检查配置")
    else:
        logging.info("Telegram 通知未配置")
    logging.info("=" * 50)

    with sync_playwright() as playwright:
        context_kwargs: dict[str, Any] = dict(
            user_data_dir=str(config.user_data_dir),
            headless=config.headless,
            viewport={"width": 1400, "height": 900},
        )
        if config.slow_mo is not None:
            context_kwargs["slow_mo"] = config.slow_mo
        if config.browser_args:
            context_kwargs["args"] = list(config.browser_args)
        context = playwright.chromium.launch_persistent_context(**context_kwargs)
        page = context.new_page()
        watcher = ConsulateWatcher(page, config)
        try:
            watcher.run()
        except KeyboardInterrupt:
            print("\n\n程序被用户中断。")
            print("浏览器窗口将保持开启10秒，方便您查看当前状态。")
            print("您可以按 Ctrl+C 再次中断来立即关闭。\n")
            try:
                time.sleep(10)
            except KeyboardInterrupt:
                print("立即关闭...")
        finally:
            print("正在关闭浏览器...")
            context.close()
            print("程序已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
