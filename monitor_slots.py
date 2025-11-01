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
import sys
import time
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import (  # type: ignore[import-untyped]
    Browser,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

HOME_URL = "https://consulat.gouv.fr/ambassade-de-france-a-rome/rendez-vous"
SESSION_MODAL_TEXT = "Une session valide contenant les informations de votre formulaire"
INFO_STEP_HEADING = "Informations importantes"
SERVICES_HEADING = "Pour combien de personnes souhaitez-vous prendre rendez-vous ?"
CONSENT_TEXT = "J'ai bien lu l'ensemble des informations disponibles sur le site du Consulat"
UNAVAILABLE_TEXT = "Aucun rendez-vous n’est disponible pour le moment"
SUCCESS_SOUND_DELAY = 0.3


@dataclass
class Config:
    headless: bool
    poll_delay: float
    slow_mo: Optional[int]


def beep(times: int = 1, delay: float = SUCCESS_SOUND_DELAY) -> None:
    """Emit an audible alert in the terminal."""

    for _ in range(times):
        # ASCII bell character works on most terminals.
        print("\a", end="", flush=True)
        time.sleep(delay)


class ConsulateWatcher:
    def __init__(self, page: Page, config: Config) -> None:
        self.page = page
        self.config = config

    def run(self) -> None:
        """Main monitoring loop."""

        attempt = 0
        while True:
            attempt += 1
            print(f"\n=== 尝试第 {attempt} 次 ===")
            self.navigate_home()
            try:
                if self.process_flow():
                    beep(times=5)
                    print("检测到页面不再显示无预约提示，程序已停止。")
                    return
                print(
                    f"页面仍显示暂无预约信息，将在 {self.config.poll_delay:.0f} 秒后重新尝试。"
                )
            except PlaywrightTimeoutError as exc:
                beep(times=2)
                print(f"❌ 操作超时：{exc}. 将在 {self.config.poll_delay:.0f} 秒后重试。")
            time.sleep(self.config.poll_delay)

    # ----- Step helpers -------------------------------------------------
    def navigate_home(self) -> None:
        print("打开预约首页……")
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(500)

    def process_flow(self) -> bool:
        button = self.page.get_by_role("button", name="Accéder aux services")
        print("点击“Accéder aux services”按钮……")
        button.click()

        if self.handle_resume_modal():
            print("检测到会话恢复对话框，已点击“Oui”。")
            return self.confirm_information()

        if self.wait_for_information_step(short_timeout=True):
            print("直接进入信息确认页面。")
            return self.confirm_information()

        print("按钮点击后页面无反应，可能触发 Cloudflare 验证。")
        self.await_cloudflare_validation()
        button = self.page.get_by_role("button", name="Accéder aux services")
        print("验证完成后再次点击“Accéder aux services”。")
        button.click()
        self.wait_for_services_step()
        self.prepare_service_selection()
        return self.confirm_information(require_manual_consent=True)

    def handle_resume_modal(self) -> bool:
        modal = self.page.locator(f"text={SESSION_MODAL_TEXT}")
        try:
            modal.wait_for(state="visible", timeout=1500)
        except PlaywrightTimeoutError:
            return False
        modal.get_by_role("button", name="Oui").click()
        return True

    def wait_for_information_step(self, short_timeout: bool = False) -> bool:
        timeout = 1500 if short_timeout else 8000
        locator = self.page.locator(f"text={INFO_STEP_HEADING}")
        try:
            locator.wait_for(state="visible", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            return False

    def await_cloudflare_validation(self) -> None:
        beep(times=3)
        print(
            "⚠️ 请手动完成页面中的 Cloudflare 人机验证。完成后按 Enter 继续。"
        )
        input("完成验证了吗？按 Enter 继续……")

    def wait_for_services_step(self) -> None:
        print("等待服务选择页面加载……")
        self.page.wait_for_selector(f"text={SERVICES_HEADING}", timeout=10000)

    def prepare_service_selection(self) -> None:
        print("将人数和“Demande de visa”数量设置为 2……")
        inputs = self.page.locator("input[type=number]")
        self.page.wait_for_timeout(300)
        if inputs.count() < 2:
            raise PlaywrightTimeoutError("未找到足够的数字输入框")

        for index in range(2):
            self._force_number(inputs.nth(index), "2")
        self.page.wait_for_timeout(300)

        # Double-check that the second service block is also 2.
        second_value = inputs.nth(1).input_value()
        if second_value != "2":
            raise RuntimeError("Demande de visa 数量未成功设置为 2")

        confirm = self.page.get_by_role("button", name="Confirmer")
        confirm.click()
        self.wait_for_information_step()

    def _force_number(self, locator, value: str) -> None:
        locator.click()
        locator.fill(value)
        locator.evaluate(
            "(el, target) => {\n"
            "  el.value = target;\n"
            "  el.dispatchEvent(new Event('input', { bubbles: true }));\n"
            "  el.dispatchEvent(new Event('change', { bubbles: true }));\n"
            "}",
            value,
        )

    def confirm_information(self, require_manual_consent: bool = False) -> bool:
        print("处理信息确认页面……")
        checkbox = self.page.locator("input[type=checkbox]").first
        checkbox.wait_for(state="attached", timeout=5000)
        if require_manual_consent or not checkbox.is_checked():
            print("勾选确认框。")
            checkbox.check()
        else:
            print("确认框已自动勾选。")

        button = self.page.get_by_role("button", name="Prendre rendez-vous")
        button.wait_for(state="enabled")
        button.click()
        return self.evaluate_calendar()

    def evaluate_calendar(self) -> bool:
        print("等待预约日历加载……")
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(500)

        unavailable = self.page.locator(f"text={UNAVAILABLE_TEXT}")
        with contextlib.suppress(PlaywrightTimeoutError):
            unavailable.wait_for(state="visible", timeout=3000)

        if unavailable.is_visible():
            print("页面显示暂无预约，准备刷新。")
            return False

        print("未检测到常见的“暂无预约”提示，可能出现可预约时段！")
        return True


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
        default=30.0,
        help="每次检测之间的等待时间（秒）",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=None,
        help="为调试添加 Playwright slow_mo（毫秒）",
    )
    args = parser.parse_args(argv)
    return Config(headless=args.headless, poll_delay=args.poll_delay, slow_mo=args.slow_mo)


def main(argv: Optional[list[str]] = None) -> int:
    config = parse_args(argv or sys.argv[1:])
    with sync_playwright() as playwright:
        browser_args = dict(headless=config.headless)
        if config.slow_mo is not None:
            browser_args["slow_mo"] = config.slow_mo
        browser: Browser = playwright.chromium.launch(**browser_args)
        context = browser.new_context()
        page = context.new_page()
        watcher = ConsulateWatcher(page, config)
        try:
            watcher.run()
        finally:
            context.close()
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
