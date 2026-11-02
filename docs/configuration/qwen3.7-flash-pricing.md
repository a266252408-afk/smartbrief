# qwen3.7-flash 固定最低档估价

配置日期：2026-11-02。用户明确要求“就按最低档即可”，因此不执行官网长输入阶梯。

官方来源：[模型价格](https://help.aliyun.com/zh/model-studio/qwen3-7-flash)、[阶梯规则](https://help.aliyun.com/zh/model-studio/model-pricing)。当前北京地域实时文本调用，最低档适用输入不超过32000Token；官方通常按整次输入量选档。本项目按用户覆盖要求固定为人民币/百万Token：普通输入0.2、输出0.8、隐式缓存命中输入0.04。

实际配置在忽略的backend/.env：MODEL_PRICES_JSON中的primary:qwen3.7-flash。公开配置见qwen3.7-flash-beijing-prices.json。不改模型Key，不覆盖其他映射，不重估历史。长输入也按最低档估价，可能低于真实账单；不含账号优惠、免费额度或其他计费项。不使用显式缓存或Batch单价。

此项只配置普通任务用量账本的估价。真实评测表单本次消费确认仍使用表单中明确填写的单价与上限，不将本配置当成消费授权。
