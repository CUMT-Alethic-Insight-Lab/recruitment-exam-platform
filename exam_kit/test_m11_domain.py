import os
import tempfile
import unittest

import exam_server


class DomainFoldersTest(unittest.TestCase):
    def _cfg(self, workdir):
        return {
            "agent_workdir": workdir,
            "agent": "opencode",
            "exam_port": 18998,
        }

    def test_sanitize_dir_strips_illegal(self):
        self.assertEqual(exam_server._sanitize_dir("语言/LLM（偏架构）"),
                         "语言-LLM")
        self.assertEqual(exam_server._sanitize_dir("VLM/MLLM（偏对齐）"),
                         "VLM-MLLM")
        self.assertEqual(exam_server._sanitize_dir("a<b>c:d|e"), "a-b-c-d-e")
        self.assertEqual(exam_server._sanitize_dir(""), "领域")

    def test_ensure_folders_creates_and_idempotent(self):
        base = tempfile.mkdtemp()
        workdir = os.path.join(base, "考试工作目录")
        cfg = self._cfg(workdir)
        labels, dirs = exam_server.ensure_domain_folders(
            cfg, ["llm_arch", "cv_arch", "agent"])
        self.assertEqual(dirs, ["语言-LLM", "CV", "Agent 编排与框架"])
        for d in dirs:
            self.assertTrue(os.path.isdir(os.path.join(workdir, d)))
        # 幂等：再跑一次不报错、不新增
        _, dirs2 = exam_server.ensure_domain_folders(cfg, ["llm_arch", "cv_arch"])
        self.assertEqual(dirs2, ["语言-LLM", "CV"])

    def test_ensure_folders_unknown_key_falls_back_to_key(self):
        base = tempfile.mkdtemp()
        workdir = os.path.join(base, "考试工作目录")
        labels, dirs = exam_server.ensure_domain_folders(
            self._cfg(workdir), ["unknowndomain"])
        self.assertEqual(labels, ["unknowndomain"])
        self.assertEqual(dirs, ["unknowndomain"])
        self.assertTrue(os.path.isdir(os.path.join(workdir, "unknowndomain")))

    def test_empty_domains_no_folders(self):
        base = tempfile.mkdtemp()
        workdir = os.path.join(base, "考试工作目录")
        labels, dirs = exam_server.ensure_domain_folders(self._cfg(workdir), [])
        self.assertEqual(labels, [])
        self.assertEqual(dirs, [])


class SanitizeEdgeTest(unittest.TestCase):
    def test_label_with_cn_paren(self):
        # 括号前为主名；中文全角括号不违禁
        self.assertEqual(exam_server._sanitize_dir("语言/LLM（偏架构）"), "语言-LLM")


if __name__ == "__main__":
    unittest.main()
