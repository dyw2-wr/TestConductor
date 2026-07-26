# 账户锁定规则

所属系统：account-web

账户连续登录失败 5 次后必须锁定。锁定期间拒绝新的登录请求，并向用户显示明确提示。管理员完成身份复核后可以解锁；解锁后用户应能使用正确密码重新登录。

## 历史数据库验证经验

用途：过去用于确认指定测试账号是否已经被锁定。只适用于账户系统，执行前仍需由当前
测试资源确认 `accounts` 表以及 `id`、`locked` 字段可访问。

```sql
SELECT locked FROM accounts WHERE id = :account_id
```

运行参数：`:account_id` 绑定 `runtime.account_id`。这条 SQL 是可复用经验，不代表当前
环境一定允许执行，也不代表本次测试一定需要执行。
