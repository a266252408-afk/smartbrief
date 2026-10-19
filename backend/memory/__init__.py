"""记忆模块：短期记忆（LangGraph Checkpointer）+ 长期记忆（ChromaDB）。

- checkpointer.py      短期记忆：任务断点快照（backend/checkpoints.db），框架自动写入；
- long_term_memory.py  长期记忆：历史 (topic, final_report) 向量入库与相似检索。
"""
