# 经营验证助手交付补齐

用户已要求继续完成五天交付清单中剩余的表单、真实验收和求职材料。沿用 FastAPI、原生前端、Dify、DeepSeek 和独立飞书 DEMO 表。

## 交付范围

- 10 个预置虚构样例可以填入可编辑的经营资料表单。提交时需确认只使用虚构资料；服务端限制字段长度、拒绝额外字段及明显联系方式/密钥。提示明确说明检测不能保证识别所有个人信息。
- 分析请求仍指定已知 scenario_id 和 UUID，可附 draft。draft 含 title、industry、offering、target_customer、business_problem、current_metrics、budget_range、goal、time_limit、fictional_only=true。快照写入飞书“资料快照”文本字段。同一 UUID 的内容变更返回 request_content_conflict，不覆盖已有记录。
- 成功响应包含仅授权该请求的 access_token，使用服务器密钥 HMAC 签名，不含服务商凭证。浏览器保存最近成功记录，刷新后可恢复查看报告和七天日志。
- POST /api/record 接收 request_id、access_token，返回已保存分析、scenario、executions。POST /api/actions 还接收 execution={day,status,observed_metric,conclusion}；状态 pending/completed/skipped。completed 需指标与结论。服务端写入 updated_at，按第1天执行记录至第7天执行记录分别更新，避免不同天相互覆盖。不再次调用 AI。
- 执行日志也仅供虚构演示，不声称已经做过七天真实经营实验。拥有恢复令牌即可访问该记录；不公开其他记录列表。
- 真实验收通过本地 FastAPI API 调用实际 Dify 和飞书，跑10个不同虚构资料，核对结果、已保存快照、重试幂等和执行日志。公开站点额外验证一次完整体验；生产限流保持开启。
- 更新README、案例、失败说明、用户教程；提供诚实的个人职责与AI辅助说明，以及可复制的简历项目条目。若找不到真实简历，不宣称已修改简历原件。

## 实现选择

服务器用飞书应用密钥派生记录令牌，避免增加账户配置；密钥轮换后旧令牌失效。输入存快照，不更改现有样例目录。新飞书字段通过幂等维护脚本补齐。生产写入端点需要各自适当的WAF限流。

## 验收

覆盖改动资料确实传入工作流、不同资料同UUID冲突、敏感内容拒绝、伪造令牌拒绝、缺失记录、执行日志编辑与不同天独立保存；桌面与390px手机浏览器检查填写、错误反馈、恢复与执行记录。
