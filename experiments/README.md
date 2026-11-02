# experiments/（数据目录，内容不进仓库）

每个子目录是一个 exp_analyzer 实验，放 `patch.json` + 4 个 JSON（base/modified 的 input/output）。
模板见 [rps_tools/exp_analyzer/sample_patch.json](../rps_tools/exp_analyzer/sample_patch.json)。

本目录下除本说明外的所有文件都被 `.gitignore` 排除（含求解数据和生成的报告），请勿强制添加。
本地回归测试（tests/test_web.py）会自动使用这里数据量最小的完整实验；没有数据时跳过。
