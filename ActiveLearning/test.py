import os
from dashscope import MultiModalConversation
import dashscope

# 以下为华北2（北京）地域的URL，各地域的URL不同。调用时请将{WorkspaceId}替换为真实的业务空间ID。
dashscope.base_http_api_url = 'https://ws-hgxzyx31pw18igy0.cn-beijing.maas.aliyuncs.com/api/v1'
messages = [
    {'role': 'system', 'content': [{'text': 'You are a helpful assistant.'}]},
    {'role': 'user', 'content': [{'text': '你是谁？'}]}
]
response = MultiModalConversation.call(
    # 若没有配置环境变量，请用阿里云百炼API Key将下行替换为：api_key = "sk-xxx",
    api_key="sk-ws-H.PLLLPDI.nFX4.MEQCIFdnR79Gp4lViVTKzEGPdUz7qIAwlXPJtwhNeSOHvmDyAiBrlTgw1egPx6NTS5wMkDT4jRXmuaISEzS90iQ8bhZ-sw",
    model="qwen3.7-plus",   # 模型列表：https://help.aliyun.com/model-studio/getting-started/models
    messages=messages,
)

if response.status_code == 200:
    print(response.output.choices[0].message.content[0]["text"])
else:
    print(f"HTTP返回码：{response.status_code}")
    print(f"错误码：{response.code}")
    print(f"错误信息：{response.message}")
    print("请参考文档：https://help.aliyun.com/model-studio/developer-reference/error-code")