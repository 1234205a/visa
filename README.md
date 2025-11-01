# 法国驻罗马领馆预约自动化脚本

本项目提供一个基于 [Playwright](https://playwright.dev/python/) 的自动化脚本，用来按照指定步骤反复检查 “https://consulat.gouv.fr/ambassade-de-france-a-rome/rendez-vous” 页面是否放出新的预约名额。

## 环境准备

1. 安装 Python 3.9+ 并创建虚拟环境（可选但推荐）：
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows PowerShell 请使用 .venv\Scripts\Activate.ps1
   ```
2. 安装依赖并准备浏览器：
   ```bash
   pip install playwright
   playwright install chromium
   ```

> ⚠️ 首次运行 Playwright 会下载 Chromium 浏览器，体积较大，请确保网络通畅。

## 使用方式

```bash
python monitor_slots.py
```

运行后会弹出一个可视化的 Chromium 浏览器窗口，脚本会自动执行以下流程：

1. **打开首页并点击 “Accéder aux services”**。
   - 如果 1 秒内弹出 “Une session valide … souhaitez-vous reprendre … ?” 对话框，会自动点击蓝色的 “Oui” 并进入下一步。
   - 如果按钮没有反应，会响铃并提示你手动完成页面中的 Cloudflare 人机验证。
2. **Cloudflare 验证**（仅在需要时）：
   - 听到提示音后，请手动勾选 Cloudflare 验证，直到出现绿色对号和 “成功”。
   - 回到终端按下回车，脚本会继续点击 “Accéder aux services”。
3. **服务选择页（图 5）**：
   - 脚本会将 “Pour combien de personnes …” 和 “Demande de visa” 的数量都设置为 `2`，然后点击 “Confirmer”。
4. **信息确认页（图 3）**：
   - 如果对号未自动勾选，脚本会帮你勾选 “J'ai bien lu …”。
   - 接着点击 “Prendre rendez-vous”。
5. **预约日历（图 4）**：
   - 若看到 “Aucun rendez-vous n’est disponible pour le moment …” 提示，会刷新回到首页，等待一段时间后重新开始。
   - 如果该提示消失，则认为可能放出了可预约时间，脚本会连续响铃并停止运行。

整个流程会无限循环，直到页面不再显示“暂无预约”的提示。此时需要你自行查看日历页并尽快完成预约。

## 常用命令行参数

- `--poll-delay <秒数>`：每次检查之间的等待时间，默认 `30` 秒。
- `--headless`：以无头模式运行（不会显示浏览器窗口），仅推荐在完全熟悉脚本行为后使用。
- `--slow-mo <毫秒>`：为调试放慢每一步操作，例如 `--slow-mo 500`。

示例：
```bash
python monitor_slots.py --poll-delay 45 --slow-mo 300
```

## 注意事项

- 网站可能随时调整布局或文案；如脚本无法定位到按钮或文本，请根据浏览器提示修改选择器。
- Cloudflare 验证环节无法自动处理，仍需人工介入。
- 建议将终端音量调高，便于听到提示音。
- 若想快速停止脚本，可在终端按 `Ctrl+C`。

祝顺利约到理想的时间！
