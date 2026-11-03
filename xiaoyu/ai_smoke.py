"""Offline portable-package check for the independent finance assistant.

Only synthetic data and an in-memory API double are used. No network request is
made, and no user's credential, accounting settings or files are read.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path


def run_ai_smoke(app) -> dict:
    from .ai_service import AIResult, FinanceAIClient, GeneratedFile, UploadedFile
    from .ai_settings import AISettings, UsageLedger, load_settings, save_settings
    from .ai_ui import FinanceAIPage

    with tempfile.TemporaryDirectory(prefix="xiaoyu-ai-smoke-") as directory:
        folder = Path(directory)
        source = folder / "合成费用.csv"
        original = "部门,金额\n研发,10\n研发,20\n".encode("utf-8-sig")
        source.write_bytes(original)
        settings = AISettings(api_key="smoke-not-a-real-key")
        # Exercise bundled TLS setup and DPAPI without sending any request.
        FinanceAIClient(settings)
        save_settings(settings, folder / "settings.json")
        assert settings.api_key not in (folder / "settings.json").read_text(encoding="utf-8")
        assert load_settings(folder / "settings.json").api_key == settings.api_key
        ledger = UsageLedger(folder / "usage.json")
        calls = []

        class OfflineClient:
            def __init__(self, options):
                self.options = options

            def test_connection(self):
                return "离线模拟连接通过"

            def upload_file(self, path, cancel=None):
                assert Path(path) == source
                assert source.read_bytes() == original
                calls.append("upload")
                return UploadedFile("file-smoke", source.name, len(original))

            def send(self, message, files, history, container_id=None,
                     web_search=False, cancel=None, on_text=None, on_status=None):
                assert [item.file_id for item in files] == ["file-smoke"]
                assert "汇总" in message and not web_search
                calls.append("send")
                if on_status:
                    on_status("正在处理合成测试资料")
                text = "合成费用合计 30.00 元，已生成汇总文件。"
                if on_text:
                    on_text(text)
                return AIResult(
                    text=text,
                    files=[GeneratedFile("cntr_smoke", "cfile_smoke", "合成汇总.csv")],
                    input_tokens=100, output_tokens=20, container_sessions=1,
                    container_id="cntr_smoke",
                )

            def download_file(self, file, cancel=None):
                return "部门,金额\n研发,30\n".encode("utf-8-sig")

            def delete_file(self, file_id):
                calls.append("cleanup")

        page = FinanceAIPage(settings=settings, ledger=ledger,
                             client_factory=OfflineClient,
                             settings_path=folder / "page-settings.json")
        try:
            page.add_paths([str(source)])
            assert page.attachment_list.count() == 1
            page.message_edit.setPlainText("按部门汇总费用，并生成表格")
            page.send_button.click()
            deadline = time.monotonic() + 8
            while page.result_list.count() != 1 and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            app.processEvents()
            assert page.result_list.count() == 1, page.status_label.text()
            assert "30.00" in page.transcript.toPlainText()
            assert calls[:2] == ["upload", "send"]
            assert ledger.summary()["requests"] == 1
            assert source.read_bytes() == original
        finally:
            page.shutdown()
            page.close()
            page.deleteLater()
            app.processEvents()
    return {"mode": "offline_mock", "model": "gpt-6-luna",
            "upload_reply_generated_file": "passed", "source_unchanged": "passed",
            "credential_encryption": "passed", "usage_ledger": "passed"}
