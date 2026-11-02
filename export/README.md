# export/（数据目录，内容不进仓库）

放 rps_solver 导出的 `*_model_input*.json` / `*_model_output*.json` / `*_proto_expression.txt`，
供诊断工具和本地回归测试使用。

本目录下除本说明外的所有文件都被 `.gitignore` 排除，请勿强制添加。
本地回归测试会自动配对 `*input*.json` 与 `*output*.json`（文件名中 input/output 互换即为一对）；没有数据时跳过。
