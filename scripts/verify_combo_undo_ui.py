"""界面验证：方向键改完之后「保存」这件事在界面上说不说得清。

用法：python scripts/verify_combo_undo_ui.py
前提：后端指到库副本（WT_DB_URL=data/verify_ui3.db）、前端 5173 在跑。

盯三件事：
  1. 按一下方向键，顶部立刻出现「✓ 已保存 HH:MM:SS」+ 改了哪一步；
  2. 点「撤销」能真的退回原位（读屏 + 接口两侧核对）；
  3. Ctrl+Z 和按钮等价，且没有可撤销步骤时不报错。

**全程跑在库副本上**：这个脚本会真的写库，指向生产库就等于拿真实数据当试验品。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:5173"
API = "http://127.0.0.1:8000"
SHOTS = Path(__file__).resolve().parent.parent / "logs" / "shots"
PROD = "YUJ-18BZ"
TARGET = "刀具Ф5.0"

PASS = 0
FAIL: list[str] = []


def check(cond, msg: str) -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  OK   {msg}")
    else:
        FAIL.append(msg)
        print(f"  FAIL {msg}")


def get_token() -> str:
    body = json.dumps({"username": "admin", "password": "admin123"}).encode()
    req = urllib.request.Request(
        f"{API}/api/auth/login", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["token"]


def api_tree(token: str) -> dict:
    req = urllib.request.Request(
        f"{API}/api/combos/tree?product={PROD}&structure=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def chain_of(node: dict, name: str, trail: list[str] | None = None) -> list[str] | None:
    trail = trail or []
    cur = trail + [node.get("part") or ""]
    if (node.get("part") or "") == name:
        return cur
    for k in node.get("children") or []:
        hit = chain_of(k, name, cur)
        if hit:
            return hit
    return None


def api_chain(token: str, name: str) -> list[str] | None:
    data = api_tree(token)
    prod = next((p for p in data["products"] if p["product"] == PROD), None)
    for root in (prod or {}).get("roots") or []:
        hit = chain_of(root, name)
        if hit:
            return hit
    return None


def main() -> int:
    token = get_token()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        page.goto(f"{BASE}/#/login")
        page.evaluate("(t) => localStorage.setItem('wt_token', t)", token)
        page.goto(f"{BASE}/#/combos?view=tree")
        page.wait_for_selector(".combo-tree", timeout=20000)
        page.fill(".tools input", PROD)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        page.click("text=全部展开")
        page.wait_for_timeout(800)

        check(page.locator(".sv-chip").count() == 0, "刚进页面没有「已保存」回执（还没改过）")
        check("没有「保存」按钮" in page.inner_text(".tree-tip"), "提示条说清了没有保存按钮")

        start = api_chain(token, TARGET)
        check(start is not None, f"接口侧能看到「{TARGET}」（{' › '.join(start or [])}）")

        # ---- 按一下 →，必须立刻出现保存回执
        row(page, TARGET).click()
        page.wait_for_timeout(300)
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(1800)
        chip = page.locator(".sv-chip.done")
        check(chip.count() == 1, "按完 → 顶部出现「已保存」回执")
        text = chip.inner_text() if chip.count() else ""
        check(bool(re.search(r"\d{2}:\d{2}:\d{2}", text)), f"回执带具体时间（{text}）")
        check("挂到" in text, f"回执说明了这一步改了什么（{text}）")
        check(page.locator(".sv-undo").count() == 1, "回执旁边有「撤销」")
        after = api_chain(token, TARGET)
        check(after and after != start, f"接口侧确实动了（{' › '.join(after or [])}）")
        page.screenshot(path=str(SHOTS / "110_saved_chip.png"))

        # ---- 点撤销，回到原位
        page.click(".sv-undo")
        page.wait_for_timeout(1800)
        back = api_chain(token, TARGET)
        check(back == start, f"撤销后回到原位（{' › '.join(back or [])}）")
        text2 = page.locator(".sv-chip.done").inner_text()
        check("撤回" in text2, f"回执改口说「已撤回」（{text2}）")
        page.screenshot(path=str(SHOTS / "111_undo.png"))

        # ---- Ctrl+Z
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(1600)
        mid = api_chain(token, TARGET)
        check(mid != start, "再按一次 → 又挂进去了")
        page.keyboard.press("Control+z")
        page.wait_for_timeout(1800)
        z = api_chain(token, TARGET)
        check(z == start, f"Ctrl+Z 也能退回原位（{' › '.join(z or [])}）")

        # ---- 清空栈之后再按 Ctrl+Z，不该崩、不该乱写
        page.keyboard.press("Control+z")
        page.wait_for_timeout(800)
        still = api_chain(token, TARGET)
        check(still == start, "没有可撤销步骤时按 Ctrl+Z 不产生副作用")

        # ---- 连做两步再连撤：栈是后进先出，撤两次两个件都要回原位
        # 拿两个件各挂一次（同一件连按两次 ↓ 会撞到「已在最后」，不该拿边界当验证）
        second = "刀具去刺打花"
        start2 = api_chain(token, second)
        row(page, TARGET).click()
        page.wait_for_timeout(300)
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(1600)
        row(page, second).click()
        page.wait_for_timeout(300)
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(1600)
        deep2 = api_chain(token, second)
        check(deep2 != start2, f"第二个件也挂进去了（{' › '.join(deep2 or [])}）")
        page.screenshot(path=str(SHOTS / "112_multi_undo.png"))
        page.keyboard.press("Control+z")
        page.wait_for_timeout(1500)
        page.keyboard.press("Control+z")
        page.wait_for_timeout(1500)
        final2 = api_chain(token, second)
        final = api_chain(token, TARGET)
        check(final2 == start2, f"撤两次后第二个件回原位（{' › '.join(final2 or [])}）")
        check(final == start, f"第一个件也回原位（{' › '.join(final or [])}）")

        browser.close()

    print(f"\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    for x in FAIL:
        print(f"  FAIL {x}")
    return 1 if FAIL else 0


def row(page, name: str):
    """按整名精确命中一行（「刀具」不能顺手点到「刀具Ф5.0」）。"""
    pat = re.compile(f"^{re.escape(name)}$")
    return page.locator(".combo-tree .tn-name").filter(has_text=pat).first


if __name__ == "__main__":
    sys.exit(main())
