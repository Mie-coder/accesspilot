# 模型密钥只能留在服务端

> AccessPilot 跨课程学习记录。

学习者已能判断 `DEEPSEEK_API_KEY` 应只放在后端本地 `.env`：前端代码和浏览器请求对用户可见，不能承担秘密保存职责。这个边界允许后续把 React 限定为调用自有 FastAPI，而由后端调用 DeepSeek。

## Evidence

学习者回答：“肯定存在后端本地.env，因为更安全。”
