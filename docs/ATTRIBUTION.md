# 代码来源与适配记录

## RAG 模块

原有源码说明记录：`backend/rag/` 的加载器、分块器、嵌入接口及向量存储设计与部分实现复用自 `zhiqida-rag` 的 `core/loaders`、`core/chunkers`、`core/embeddings`、`core/vectorstores`。

SmartBrief 中的适配包括 EMBEDDING_* 配置、backend/data 数据目录、内容 md5 稳定 ID，以及模板、术语、历史报告的应用层集成。

当前仓库未附上述来源的完整仓库地址和许可信息，本记录不推定其许可，也不将复用部分描述为独立原创。

## 自定义技能与依赖

`backend/skills_pool/humanizer/` 中保留了该技能自身的来源说明；本次文档整理不移除第三方归属信息。

Python 与前端依赖分别见 requirements.txt 与 package.json，其许可由对应依赖声明。
