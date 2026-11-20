# Elements of Style 检查器（一条规则一个 agent）

[English](README.md) | 简体中文

上传一份英文 PDF，按 William Strunk Jr. 的 *The Elements of Style*（1918 年原版，公有领域）逐条检查：
**每条规则一个 agent，每个 agent 只管自己那一条，从文档第一页读到最后一页**，最后标出哪段文字违反了哪条规则。

输出三样东西：

| 文件 | 内容 |
|---|---|
| `report.html` | 可筛选的报告：按规则/严重程度/关键词过滤，原句里高亮问题片段，附原因和修改建议；右上角可切换 English/中文、深色/浅色 |
| `<原文件名>.annotated.pdf` | 原 PDF 的副本，问题处直接加了淡色高亮批注（颜色按章节区分，批注里写规则号、原因、建议，语言跟 `--lang`） |
| `report.json` | 全部结果和每个 agent 的运行统计，方便接其他程序 |

## 快速开始

```powershell
pip install -r requirements.txt
```

需要一个 OpenAI 兼容的模型接口（vLLM、Ollama、DeepSeek 等都行）。开发和测试用的是 vLLM 部署的 Qwen3-VL-8B-Instruct。

1. 如果模型跑在远程服务器上，先开 SSH 隧道（单独一个终端，一直开着）：

```powershell
ssh -N -L 8000:127.0.0.1:8000 <用户名>@<服务器>
```

2. 把 `.env.example` 复制成 `.env`，填上接口地址、API key 和模型名。
3. 测试连接：

```powershell
python -m eos_checker ping
```

4. 检查 PDF：

```powershell
python -m eos_checker check 你的文件.pdf --lang zh
```

结果默认写到 PDF 旁边的 `<文件名>_style_check/` 文件夹。
说明文字默认是英文，加 `--lang zh` 让 agent 和批注 PDF 用中文。HTML 报告无论哪种都能在中英文之间切换。

## 常用命令

| 命令 | 作用 |
|---|---|
| `python -m eos_checker rules` | 列出所有规则（加 `--all` 包括过时的和不可检查的） |
| `python -m eos_checker extract x.pdf` | 看 PDF 被切成了哪些段落（排查提取问题用） |
| `python -m eos_checker check x.pdf --dry-run` | 不调用模型，只估算每个 agent 要调用多少次 |
| `python -m eos_checker check x.pdf --no-llm` | 只跑纯正则规则（几秒钟，不需要隧道） |
| `--pages 1-20,25` | 只查这些页 |
| `--rules II-05,III-10,V-etc` / `--sections II,III` | 只跑指定规则 / 章节 |
| `--include-outdated` | 也跑“现代用法已不认同”的规则（见下） |
| `--lang zh` | agent 用中文写原因、批注 PDF 用中文（默认 `en` 英文）。跑完后会把所有原因翻译成另一种语言（几次模型调用），所以 HTML 报告两种语言都能切换 |
| `--chunk-chars 2000` | 每块字符数。小模型用小块召回更好，但调用更多 |
| `--concurrency 16` / `--window 8` | 同时在途的请求数 / 单个 agent 并行读几块（`--window 1` = 严格按顺序） |
| `--no-double-check` | 关掉复核（结果更多，误报也更多） |
| `--no-cache` | 不用缓存（默认会缓存模型回答到 `.cache/`，重跑同一文件几乎不花时间） |

## 结构

```
PDF ──► document.py   提取文字和每个字符的坐标；去页眉页脚页码；
         │            跨行/跨栏/跨页的段落拼回去；行末断词拼回并记录；识别标题；切句
         ▼
       chunker.py    按段落/句子切块（默认 2000 字符），每块附上前文作为只读上下文
         ▼
       agents.py     80 个 RuleAgent 并发运行，每个只负责一条规则，各自从头读到尾：
         │             ① 正则命中直接报告（拼写、different than、as to whether ……）
         │             ② 正则找候选句 → 模型逐个确认（被动语态、less/fewer ……）
         │             ③ 模型通读全文找问题 → 同一 agent 再复核一遍，确认的才保留
         ▼
       translate.py  把原因、违规类型翻成另一种语言（引号里的英文原样保留，翻不好就保留原文）
         ▼
       report.py     report.json / report.html（中英可切换）/ 批注版 PDF
```

规则全部在 [rules/elements_of_style_1918.yaml](rules/elements_of_style_1918.yaml)，代码里没有写死任何规则。改规则、加规则只需要改这个文件。

## 规则是怎么从原书“翻译”过来的

原书（Project Gutenberg #37134）共 6 章，转成了 91 条规则：

| 章 | 条数 | 内容 |
|---|---|---|
| II 用法基本规则 | 7 | 所有格 's、牛津逗号、插入语逗号、连词前逗号、逗号粘连、句子碎片、悬垂修饰 |
| III 写作基本原则 | 11 | 段落、主题句、主动语态、肯定形式、具体语言、删冗词、松散句、平行结构、相关词放一起、摘要时态、句末强调 |
| IV 格式问题 | 6 | 数字、括号标点、引文、引用出处、行末断词（标题格式 1 条无法从 PDF 文字判断，不运行） |
| V 常被误用的词语 | 63 | 每个词条一个 agent：all right、different than、due to、etc.、less/fewer、like/as、while、whom …… |
| VI 拼写 | 4 | 原书易错词表（加常见错法）、双写辅音、to-day 等 |

每条规则的 YAML 里有：原书规则的意思（`description`）、编号的“违规类型”（`violations`）、明确“不算违规”的情况（`not_violations`，原书里的例外都写进去了）、原书的正误例句（`examples`）、正则触发器（`triggers`）。

**10 条规则标为 `outdated`**（默认不跑，`--include-outdated` 才跑）：原书写于 1918 年，有些要求现代英语已经不认同了，比如 each/everyone 后面必须用 he 不能用 they、第一人称将来时必须用 shall、不能分裂不定式、要写 to-day。这些规则忠实保留了原文，但默认跑会产生大量“正确但过时”的提示。

## 为 8B 小模型做的精度措施

直接让 Qwen3-VL-8B 通读全文找问题，测试样本里误报很多（78 条里大约一半是错的）。现在的做法：

1. **能用正则就不用模型**：拼写错误、固定搭配等 14 条规则纯正则，零误报。
2. **正则找候选、模型只做判断**：41 条规则先用正则找出可疑句子，模型只回答“这句是不是真的违规”，比让它自己找准得多。
3. **必须选一个“违规类型”**：模型回答必须指明是规则里第几种违规，对不上的丢弃。
4. **复核**：通读类规则提出的每条问题，再让同一个 agent 单独复核一次，确认的才保留。
5. **确定性守卫**：例如“主动语态·类型 1（带 by 的被动）”要求句子里真的有“be + 过去分词 + by”；标点类规则要求修改建议确实改动了标点；“建议”和原文一样、或者模型自己说“无需修改”的结果都丢弃。
6. **先写推理字段再下结论**：如逗号粘连要求模型先判断逗号前后是不是各自都是完整句子，两个都是 true 才算违规。
7. **段落级规则每段只报一次**，主观性强的规则（具体语言、句末强调等）每块最多报 2–3 条；“松散句”这类能数出来的段落问题，直接按规则数出候选段落再让模型确认。

测试样本（`samples/sample.pdf`，2 页，人为埋了约 45 个问题）：找到 50 条，人工逐条核对约 90% 正确，漏掉约 4 个（见下）；干净段落零误报。

## 已知限制

- **模型能力**：8B 模型对需要语义理解的规则有漏报。样本里漏掉的：悬垂修饰“Walking slowly down the road, the bus appeared”（它认为公交车在走路）、关系从句离先行词太远（III-16）、行末断词 des-cribe。换更强的模型（改 `.env` 里的 `LLM_MODEL` / `LLM_BASE_URL`，任何 OpenAI 兼容接口都行）能直接提高这类规则的召回。
- **偶有误报**：平行结构（III-15）、主动语态（III-10，比如把 “taught by the textbook method” 里表示工具的 by 当成施动者）。它们的严重程度标为中/低，报告里可以按严重程度过滤。
- **牛津逗号（II-02）**精度偏低：区分“引导短语, A and B”和“A, B and C”需要句法分析，8B 模型数不准并列项。
- **扫描版 PDF** 没有文字层，需要先 OCR。
- **多栏排版**按 PDF 内部顺序读取，大部分正常，个别排版可能顺序错乱（用 `extract` 命令检查）。
- 论文里的图表标题、参考文献会被当成普通文字检查，可能产生一些无意义的提示。

## 速度

32 页（约 900 句）的测试文档：约 600 次模型调用，用时约 2 分钟（约 83 万输入 token、3.6 万输出 token）。重跑同一文件会命中缓存，几乎瞬间完成。按这个比例，300 页大约 15–20 分钟。
时间主要取决于服务器吞吐；vLLM 会把并发请求合批，`--concurrency` 可以调大到 32 试试。

## 测试

```powershell
python samples/make_sample_pdf.py
python -m eos_checker check samples/sample.pdf
```

## 许可

代码以 [MIT License](LICENSE) 发布。规则内容整理自 Strunk 1918 年原版 *The Elements of Style*（Project Gutenberg #37134，公有领域）。
