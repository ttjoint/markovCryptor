"""HTTP 层：每个资源一个 APIRouter。

端点不再闭包捕获 account —— 它们从 ``app.state`` 取（见 deps.py），
这样路由器可以是模块级的，也不必把 25 个函数塞进一个 665 行的函数体。
"""
