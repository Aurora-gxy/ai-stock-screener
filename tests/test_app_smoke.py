"""页面冒烟测试：用 Streamlit AppTest 跑通「解析 → 筛选 → 结果 → 保存 → 监控」，数据为构造数据。"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FUYAO_API_KEY", "test-key")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from core import fuyao
    from tests.fakes import FakeSession
    orig = fuyao.FuyaoClient.__init__

    def patched(self, *a, **kw):
        kw.setdefault("session", FakeSession())
        kw.setdefault("sleep", lambda s: None)
        orig(self, *a, **kw)

    monkeypatch.setattr(fuyao.FuyaoClient, "__init__", patched)
    # 构造池里 PE 样本少，测试用绝对 PE 口径
    from core import metrics
    monkeypatch.setitem(metrics.UNIVERSES, "上证50", "000016.SH")
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(os.path.join(ROOT, "streamlit_app.py"), default_timeout=60)


def _btn(at, label):
    return next(b for b in at.button if label in b.label)


def test_main_flow(app):
    at = app.run()
    assert not at.exception
    _btn(at, "解析意图").click().run()
    assert not at.exception
    assert any("规则解析" in i.value for i in at.info)
    _btn(at, "执行筛选").click().run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "入选" in labels and "无法判断（数据缺失/过期）" in labels
    _btn(at, "保存当前方案").click().run()
    assert not at.exception
    _btn(at, "转为监控任务").click().run()
    assert not at.exception
    _btn(at, "立即运行一次检查").click().run()
    assert not at.exception


def test_compliance_example(app):
    at = app.run()
    at.text_area[0].set_value("帮我找明天会涨的股票，最好稳赚").run()
    _btn(at, "解析意图").click().run()
    assert not at.exception
    errs = " ".join(e.value for e in at.error)
    assert "预测" in errs and "收益承诺" in errs


def test_conflict_blocks_run(app):
    at = app.run()
    at.text_area[0].set_value("市盈率低于10倍，市盈率高于20倍").run()
    _btn(at, "解析意图").click().run()
    assert any("冲突" in e.value for e in at.error)
    assert _btn(at, "执行筛选").disabled
