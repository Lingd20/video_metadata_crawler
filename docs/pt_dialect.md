# 巴西葡语口音区划分、数据分布与 ASR WER 对比

本文汇总两套口音区划分、CORAA-MUPE-ASR 各 split 时长分布、SOW 无标爬取数据（0730 + 0804 合并）分布，以及 dolphin-moe 与 qwen3-asr 在 CORAA-MUPE-ASR **test** 上按口音区的 WER。

划分定义见 `brazilian_portuguese_dialect_taxonomy.md` 与 `dialect_region_map.json`。州代码来自 `utt2lang` / `birth_state`（如 `pt_BR-SP`）。

两套划分覆盖相同 UF 的类别使用**同一标签**（北部 Norte、东北 Nordeste、南部 Sul）。其余类别按各自粒度命名。

> 两套分类都不是巴西葡语的官方方言区划，而是面向 ASR 数据组织与评测的实用标签。方言在州界附近是连续过渡的；州级 metadata 表示说话人地理来源，不等于经过核验的口音。

特殊标签（两套共用）：**其他 Other** = 非巴西口音（`pt_PT`、`pt_CL`、标注 `other` 等）；**未知 Unknown** = 缺失/无法解析（`pt_NULL`、`pt_BR` 无州码）。

---

## 1. 划分方式

### 1.1 7 类口音区（`accent_region_7`）

按区域语音特征与方言地理学划分，用于口音分类、分区评测和数据分析。

当前数据无圣保罗「首都 / 内陆」字段，故 **SP 整州并入 Paulistano_Caipira**。GO / MS 与 Caipira–Centro-Oeste 连续体一并归入该类；PR 归入 Sul；ES 单独为 Sudeste_Transitional。

| 中文 | 标签 | 包含 UF | 说明 |
| --- | --- | --- | --- |
| 圣保罗-Caipira | Paulistano_Caipira | SP, GO, MS | 圣保罗都市口音 + 内陆 Caipira；GO/MS 为 Caipira 向中西部过渡 |
| 里约 | Carioca | RJ | 里约；词尾 /s/→[ʃ]、/t d/ 腭化等辨识度高 |
| 米纳斯 | Mineiro | MG | 大量缩减与连读（você→cê 等），书面形式与实际发音差距大 |
| 东北 | Nordeste | BA, PE, CE, AL, PB, PI, SE | 东北宏观类（内部仍有 Baiano / Pernambucano 等差异）；本次无 MA、RN |
| 北部 | Norte | PA, RO | 北部宏观类；本次无 AM、AC、RR、AP、TO |
| 南部 | Sul | RS, PR | RS 偏 Gaúcho；PR 北部与 Caipira 连续。本次无 SC |
| 东南过渡 | Sudeste_Transitional | ES | 不宜简单并入 Carioca 或 Mineiro |

### 1.2 5 类口音区（`geo_region_5`）

按 IBGE 式行政大区划分，用于地理统计、metadata 与较粗的口音区评测。与 7 类相比，东南四州合并为 Sudeste，GO/MS 单列为 Centro-Oeste；**Norte / Nordeste / Sul 的 UF 范围与 7 类相同，标签也相同**。

| 中文 | 标签 | 包含 UF | 说明 |
| --- | --- | --- | --- |
| 北部 | Norte | PA, RO | 巴西北部；本次无 AM、AC、RR、AP、TO |
| 东北 | Nordeste | BA, PE, CE, AL, PB, PI, SE | 巴西东北；本次无 MA、RN |
| 东南 | Sudeste | SP, RJ, MG, ES | 东南四州，对应 7 类中的圣保罗-Caipira（仅 SP）、里约、米纳斯、东南过渡 |
| 南部 | Sul | RS, PR | 巴西南部；本次无 SC |
| 中西部 | Centro-Oeste | GO, MS | 中西部；在 7 类中并入圣保罗-Caipira。本次无 MT、DF |

### 1.3 两套标签的对应关系

| UF | 7 类口音区 | 5 类口音区 |
| --- | --- | --- |
| SP | 圣保罗-Caipira Paulistano_Caipira | 东南 Sudeste |
| RJ | 里约 Carioca | 东南 Sudeste |
| MG | 米纳斯 Mineiro | 东南 Sudeste |
| ES | 东南过渡 Sudeste_Transitional | 东南 Sudeste |
| BA, PE, CE, AL, PB, PI, SE | 东北 Nordeste | 东北 Nordeste |
| PA, RO | 北部 Norte | 北部 Norte |
| RS, PR | 南部 Sul | 南部 Sul |
| GO, MS | 圣保罗-Caipira Paulistano_Caipira | 中西部 Centro-Oeste |

---

## 2. CORAA-MUPE-ASR 时长分布

来源：`processed/manifests/{train,dev,test}/segments`（时长 = end − start）+ `utt2lang`，统计脚本 `stats_coraa_dialect_duration.py`。  
比例 = 该区时长 / **该 split 总时长**。未出现的类别不列出（时长为 0）。

| split | 句数 | 总时长 (h) |
| --- | ---: | ---: |
| train | 276881 | 319.99 |
| dev | 9894 | 12.25 |
| test | 30968 | 32.91 |
| 合计 | 317743 | 365.15 |

### 2.1 7 类口音区

| 口音区 | train h | train % | dev h | dev % | test h | test % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Paulistano_Caipira | 199.16 | 62.24 | 4.83 | 39.42 | 5.51 | 16.75 |
| Carioca | 10.48 | 3.27 | — | — | 2.46 | 7.49 |
| Mineiro | 17.46 | 5.46 | — | — | 3.87 | 11.77 |
| Nordeste | 29.67 | 9.27 | 3.30 | 26.91 | 10.37 | 31.50 |
| Norte | 22.63 | 7.07 | 1.67 | 13.64 | 3.24 | 9.85 |
| Sul | 3.14 | 0.98 | — | — | 5.59 | 17.00 |
| Sudeste_Transitional | — | — | — | — | 1.70 | 5.18 |
| Other | 16.07 | 5.02 | 1.53 | 12.50 | — | — |
| Unknown | 21.38 | 6.68 | 0.92 | 7.52 | 0.15 | 0.47 |
| **合计** | **319.99** | **100** | **12.25** | **100** | **32.91** | **100** |

要点：

- **train 严重偏 SP**：Paulistano_Caipira 约 62%（几乎全是 SP；train 中无 GO/MS）。
- **dev 覆盖窄**：只有 SP / PE / PA，外加 Other（`pt_AR`）与 Unknown。
- **test 刻意拉开地域**：Nordeste 31.5% 为最大块；Sul 17%；Paulistano_Caipira 降至 16.8%（test 中该类 = SP + GO + MS）。ES 仅出现在 test。

### 2.2 5 类口音区

| 口音区 | train h | train % | dev h | dev % | test h | test % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Norte | 22.63 | 7.07 | 1.67 | 13.64 | 3.24 | 9.85 |
| Nordeste | 29.67 | 9.27 | 3.30 | 26.91 | 10.37 | 31.50 |
| Sudeste | 227.10 | 70.97 | 4.83 | 39.42 | 10.27 | 31.22 |
| Sul | 3.14 | 0.98 | — | — | 5.59 | 17.00 |
| Centro-Oeste | — | — | — | — | 3.28 | 9.96 |
| Other | 16.07 | 5.02 | 1.53 | 12.50 | — | — |
| Unknown | 21.38 | 6.68 | 0.92 | 7.52 | 0.15 | 0.47 |
| **合计** | **319.99** | **100** | **12.25** | **100** | **32.91** | **100** |

Norte / Nordeste / Sul 的时长与 7 类完全相同。train 中 Sudeste 占 **71%**（SP + RJ + MG）；Centro-Oeste 只在 test（GO + MS）。

---

## 3. 无标爬取数据（0730 + 0804 合并）

来源：SOW YouTube 元数据，脚本 `annotate_sow_dialect_region.py`。  
CSV 的 `dialect_state` 因 `code_switch` 编码错位到 `topic`；通过 xlsx「来源名称」= csv `channel_name` 对齐后，按频道标注的 UF 映射到 5/7 类。

| 批次 | 视频数 | 频道数 | 时长 (h) |
| --- | ---: | ---: | ---: |
| 0730（137 频道） | 6371 | 137 | 695.13 |
| 0804 | 22703 | 509 | 5455.14 |
| **合并（去重）** | **29074** | **646** | **6150.27** |

合并方式：两批视频/时长直接相加（批次不重叠）。频道数按两批相加，未做跨批次去重。0730 只有 **AL、MG**；0804 **没有 MG**。

**多地标注的计入规则**（不再保留 Mixed）：

- 能解析出 UF / `other` 时，映射到口音区后按**去重后的口音区集合**计时。
- 两地或多地属于**同一**口音区（例如 PA+RO 都是 Norte，或 ES+SP 在 5 类都是 Sudeste）：整段时长只计入该区一次。
- 属于**不同**口音区（例如 AL+MG，或 `PB+other`）：整段时长**分别计入**每一个口音区，因此各区占比之和可以超过 100%。
- 分母始终是去重总时长 **6150.27 h**。下表中的视频数、频道数同样按「出现在该区」计，一条多区视频会在多个区各计一次。

### 3.1 7 类口音区（合并）

| 口音区 | 时长 (h) | 占比 | 视频数 | 频道数 |
| --- | ---: | ---: | ---: | ---: |
| Paulistano_Caipira | 1573.20 | 25.58% | 5502 | 107 |
| Carioca | 1406.42 | 22.87% | 5166 | 37 |
| Mineiro | 373.46 | 6.07% | 4300 | 96 |
| Nordeste | 1477.90 | 24.03% | 7393 | 255 |
| Norte | 330.62 | 5.38% | 988 | 57 |
| Sul | 1678.66 | 27.29% | 8275 | 62 |
| Sudeste_Transitional | 439.13 | 7.14% | 1707 | 29 |
| Other | 1231.62 | 20.03% | 4653 | 136 |
| **计入合计（含交叉）** | **8511.00** | **138.38%** | — | — |
| **去重合计（分母）** | **6150.27** | **100%** | **29074** | **646** |

拆分 Mixed 后，Sul / Paulistano_Caipira / Nordeste / Carioca 都超过 20%；Mineiro 由原先纯净段的 2.3% 升至 6.07%（0730 的 AL+MG 交叉段计入两侧）。Other 含纯 `other` 以及 `UF+other` 的交叉计入。

### 3.2 5 类口音区（合并）

| 口音区 | 时长 (h) | 占比 | 视频数 | 频道数 |
| --- | ---: | ---: | ---: | ---: |
| Norte | 330.62 | 5.38% | 988 | 57 |
| Nordeste | 1477.90 | 24.03% | 7393 | 255 |
| Sudeste | 2537.65 | 41.26% | 12018 | 207 |
| Sul | 1678.66 | 27.29% | 8275 | 62 |
| Centro-Oeste | 952.00 | 15.48% | 3973 | 52 |
| Other | 1231.62 | 20.03% | 4653 | 136 |
| **计入合计（含交叉）** | **8208.44** | **133.46%** | — | — |
| **去重合计（分母）** | **6150.27** | **100%** | **29074** | **646** |

Norte / Nordeste / Sul / Other 与 7 类数字相同。5 类交叉更少（133% vs 138%），因为 ES+SP、RJ+SP 等在 5 类同属 Sudeste，只计一次。Centro-Oeste 在计入 GO/PR、GO/SE 等交叉后升至 15.5%。

与 CORAA train 对比：无标 Sudeste 约 41%（含交叉），不像 train 那样 71% 堆在 Sudeste；Sul、Carioca 在无标中更充足，Norte 仍然偏少。

---

## 4. dolphin-moe vs qwen3-asr：口音区 WER

评测集：CORAA-MUPE-ASR **test**（30968 句，32.91 h）。  
指标：`PortugueseTextNormalizer` + jiwer **WER**（corpus WER，按 ref 词数加权）。  
对比设置均为**指定葡语**（dolphin-moe `with_lang` / qwen3-asr `language=Portuguese`），与 `CORAA-MUPE-ASR_compare.json` 一致。

| 模型 | 权重 / 配置 | WER |
| --- | --- | ---: |
| dolphin-moe | `checkpoints/large/avg5` | 20.94% |
| qwen3-asr | Qwen3-ASR-1.7B | 18.88% |

Δ = dolphin − qwen（正值表示 dolphin 更高、更差）。

### 4.1 按 7 类口音区

| 口音区 | 句数 | dolphin WER | qwen WER | Δ |
| --- | ---: | ---: | ---: | ---: |
| Paulistano_Caipira | 4398 | 21.12 | 18.92 | +2.20 |
| Carioca | 2882 | 17.88 | 17.06 | +0.82 |
| Mineiro | 3327 | 24.93 | 21.48 | +3.45 |
| Nordeste | 10868 | 22.50 | 20.07 | +2.43 |
| Norte | 3575 | 29.41 | 26.43 | +2.99 |
| Sul | 4364 | 14.38 | 13.78 | +0.60 |
| Sudeste_Transitional | 1388 | 11.40 | 10.31 | +1.09 |
| Unknown | 166 | 35.61 | 32.98 | +2.64 |
| **全体** | **30968** | **20.94** | **18.88** | **+2.05** |
| **7 区算术平均**（不含 Unknown） | — | **20.23** | **18.29** | **+1.94** |

各区 dolphin 均高于 qwen。差距最大的是 **Mineiro**（+3.45）和 **Norte**（+2.99）；**Sul / Carioca** 最接近（+0.60 / +0.82）。两边最难的都是北部，最容易的都是 ES（Sudeste_Transitional）和南部。

Paulistano_Caipira 在 test 中混合了 SP（dolphin 23.09 / qwen 19.63）、GO（22.93 / 21.58）和较容易的 MS（9.34 / 8.14）。

### 4.2 按 5 类口音区

| 口音区 | 句数 | dolphin WER | qwen WER | Δ |
| --- | ---: | ---: | ---: | ---: |
| Norte | 3575 | 29.41 | 26.43 | +2.99 |
| Nordeste | 10868 | 22.50 | 20.07 | +2.43 |
| Sudeste | 9599 | 20.40 | 18.07 | +2.33 |
| Sul | 4364 | 14.38 | 13.78 | +0.60 |
| Centro-Oeste | 2396 | 19.74 | 18.43 | +1.31 |
| Unknown | 166 | 35.61 | 32.98 | +2.64 |
| **全体** | **30968** | **20.94** | **18.88** | **+2.05** |
| **5 区算术平均**（不含 Unknown） | — | **21.29** | **19.35** | **+1.93** |

Norte / Nordeste / Sul 的 WER 与 7 类相同。5 类下同样是 **Norte 最难、Sul 最容易**。Sudeste 把 Carioca / Mineiro / SP / ES 捆在一起，WER 介于 7 类中 Carioca 与 Mineiro 之间。Centro-Oeste（GO+MS）好于 Sudeste 整体，主要是因为 MS 错误率很低。

---

## 5. 数据文件

| 内容 | 路径 |
| --- | --- |
| 划分说明 | `datasets_processing/pt/brazilian_portuguese_dialect_taxonomy.md` |
| UF → 5/7 类映射 | `datasets_processing/pt/dialect_region_map.json` |
| CORAA 时长统计 | `datasets_processing/pt/CORAA-MUPE-ASR/dialect_duration_summary.json` |
| SOW 0730/0804 分区统计 | `datasets_processing/pt/sow_0730_0804_dialect_region_stats.json` |
| dolphin-moe test summary | `asr-models/inference_standalone/results/CORAA-MUPE-ASR_lang/summary.json` |
| qwen3-asr test summary | `asr-models/qwen3-asr/results/CORAA-MUPE-ASR/summary.json` |
| 按州对比总表 | `asr-models/inference_standalone/results/CORAA-MUPE-ASR_compare.json` |
