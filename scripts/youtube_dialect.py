#!/usr/bin/env python3
"""
发现 YouTube 上与葡萄牙语相关的博主（频道维度，主输出为频道 URL，不写视频 watch 链接）。

用途：
1) 按 docs/pt_dialect.md 的口音区筛选检索词（默认东南 Sudeste = SP+RJ+MG+ES）
2) 视频搜索 ytsearch：从命中稿件聚合 channel_id / uploader
3) 「频道」标签搜索结果页：sp=EgIQAg%3D%3D，直接发现频道
4) 导出 CSV；可选导出视频 URL / crawl_all TSV，便于后续下载音频

依赖：
  pip install yt-dlp

示例（东南地区。每个检索词先取 100 条；命中视频再写入与 run.sh 相同列的 Excel）：
  python scripts/youtube_dialect.py ^
    --region Sudeste ^
    --scheme geo5 ^
    --output results/dialect_Sudeste_channels.csv ^
    --metadata-out results/dialect_Sudeste_metadata.xlsx ^
    --video-urls-out results/dialect_Sudeste_video_urls.txt ^
    --channels-tsv-out results/dialect_Sudeste_channels.tsv

  python scripts/youtube_dialect.py --list-regions
  python scripts/youtube_dialect.py --region 东南
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
import re
import sys
import time
import traceback
import unicodedata
import urllib.parse
from typing import Any, Dict, Iterable, List, Tuple

from yt_dlp import YoutubeDL


def _configure_stdio_utf8() -> None:
    """避免 Windows GBK 控制台打印葡语重音字符时报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


# DEFAULT_QUERIES = [
#     "portuguese podcast",
#     "português entrevista",
#     "portugal notícias",
#     "brasil notícias",
#     "portuguese radio",
#     "português debate",
#     "portuguese education lecture",
#     "história em português",
#     "ciência em português",
#     "tecnologia em português",
#     "futebol brasileiro análise português",
#     "brasileirão dzé",
#     "seleção brasileira entrevista português",
#     "copa do mundo brasil narração português",
#     "big brother brasil comentários podcast",
#     "novela brasileira react português",
#     "culinária brasileira canal português",
#     "viagem brasil vlog português",
# ]

# ============================================================
# 葡语方言（按州）专用检索词
# 优先召回当地人开口说话的内容：街头采访、地方议会、本地谈话。
# 更稳的词是「方言标记词 / 内陆城市街采 / 本地 papo·podcast」，
# 而不是首都旅游 vlog、音乐曲风名，或夹英文的 query。
# 方言标记词与内陆城市见下方 EXTRA_DIALECT_QUERIES。
# ------------------------------------------------------------

# RS（Rio Grande do Sul 南里奥格兰德，巴西最南部，州府阿雷格里港 POA）
# 目标语言现象：Gaúcho（南大河方言），典型特征是 /r/ 卷舌化，"tchê/po/azz" 等感叹词。
RS_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua porto alegre",
    "entrevista rua rio grande do sul",
    "povo de poa falando",
    "gaúcho na rua entrevista",
    "tchê entrevista",

    # 2) 自媒体 / 草根 Vlog
    "vlog de porto alegre",
    "gaúcho pelo mundo vlog",
    "tchê só canal",
    "poa em foco",
    "vida no sul brasil",

    # 3) 关键词组合：方言触发词 + 地名
    "sotaque gaúcho entrevista",
    "fala gaúcha natural",
    "gaúcho falando tchê",

    # 4) 特色节日 / 文化活动
    "festa junina gaucha",
    "churrasco brasileiro entrevista",
    "farroupilha celebrations",

    # 5) 地方议会：议员发言方言密度大
    "assembleia legislativa rio grande do sul",
    "câmara municipal porto alegre sessão",
    "deputado gaúcho falando",
    "vereador de poa falando",
]


# PR（Paraná 巴拉那，巴西南部，州府库里蒂巴 Curitiba）
# 目标语言现象：Catarense/Paranaense，口音较中性但有独特词汇，"vixi/rapá/mané" 等。
PR_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua curitiba",
    "entrevista rua paraná",
    "povo de curitiba falando",
    "paranaense na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de curitiba",
    "curitiba em 5 minutos",
    "paranaense pelo mundo vlog",
    "vida no paraná",

    # 3) 关键词组合
    "sotaque paranaense entrevista",
    "curitibano falando vixi",

    # 4) 内陆子区（西部 Cascavel / 西部 Maringá / 北部 Londrina）
    "cascavel falando entrevista",
    "maringá entrevista rua",
    "londrina falando",

    # 5) 地方议会
    "assembleia legislativa paraná",
    "câmara municipal curitiba sessão",
    "deputado paranaense falando",
]


# GO（Goiás 戈亚斯，巴西中西部，州府戈亚尼亚 Goiânia）
# 目标语言现象：Goiano，中西部口音，"uai/é/né/quer" 等，接近 Mineiro 但更粗犷。
GO_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua goiânia",
    "entrevista rua goiás",
    "povo de goiânia falando",
    "goiano na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de goiânia",
    "goiânia em foco",
    "goiano pelo mundo vlog",
    "vida em goiás",

    # 3) 关键词组合
    "sotaque goiano entrevista",
    "goiano falando né",
    "fala goiana natural",

    # 4) 特色活动
    "aniversário de goiânia entrevista",
    "festa goiana entrevistas",

    # 5) 地方议会
    "assembleia legislativa goiás",
    "câmara municipal goiânia sessão",
    "deputado goiano falando",
]


# MS（Mato Grosso do Sul 南马托格罗索，巴西中西部，州府大坎普 Campo Grande）
# 目标语言现象：Sul-Mato-Grossense，近似 Goiano，"uai/é" 高频，靠近潘塔纳尔湿地有牛仔文化。
MS_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua campo grande",
    "entrevista rua mato Grosso do sul",
    "povo de campo grande falando",
    "sul mato grossense na rua",

    # 2) 自媒体 / 地方 Vlog
    "vlog de campo grande",
    "pantanal mato Grosso entrevistas",
    "vida no pantanal speaking",
    "cowboy do pantanal",

    # 3) 关键词组合
    "sotaque sul mato grossense",
    "fala pantaneira natural",

    # 4) 牛仔文化 / 农业
    "boiadeiro entrevistas pantanal",
    "cabanha mato Grosso speaking",

    # 5) 地方议会
    "assembleia legislativa mato Grosso do sul",
    "câmara municipal campo grande sessão",
    "deputado sul mato grossense falando",
]


# RO（Rondônia 朗多尼亚，巴西西北部，州府韦柳港 Porto Velho）
# 目标语言现象：Rondoniano，口音受北部方言影响，/r/ 强卷舌，词汇受亚马逊地区影响。
RO_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua porto velho",
    "entrevista rua rondônia",
    "povo de porto velho falando",
    "rondoniano na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de porto velho",
    "rondônia em foco",
    "vida em rondônia",
    "amazonas rondônia entrevistas",

    # 3) 关键词组合
    "sotaque rondoniano entrevista",
    "fala amazônica natural",

    # 4) 亚马逊 / 河流文化
    "ribeirinho entrevistas rondônia",
    "vida ribeirinha amazonas",

    # 5) 地方议会
    "assembleia legislativa rondônia",
    "câmara municipal porto velho sessão",
    "deputado rondoniano falando",
]


# PA（Pará 帕拉，巴西北部，州府贝伦 Belém）
# 目标语言现象：Paraense，典型北部方言，"mainha/vó/né/axe" 等，亚马逊河口文化。
PA_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua belém",
    "entrevista rua pará",
    "povo de belém falando",
    "paraense na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de belém",
    "belém em foco pará",
    "paraense pelo mundo vlog",
    "vida em pará",

    # 3) 关键词组合
    "sotaque paraense entrevista",
    "belenense falando né",
    "fala amazônica pará",

    # 4) 亚马逊文化 / 节日
    "círio de nazaré belém entrevistas",
    "gastronomia paraense interviews",
    "pato a zucca entrevistas",

    # 5) 地方议会
    "assembleia legislativa pará",
    "câmara municipal belém sessão",
    "deputado paraense falando",
]


# PI（Piauí 皮奥伊，巴西东北部，州府特雷西纳 Teresina）
# 目标语言现象：Piauiense，东北方言区，"oxe/visse/arretado/mainha" 等Nordestino特征明显。
PI_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua teresina",
    "entrevista rua piauí",
    "povo de teresina falando",
    "piauiense na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de teresina",
    "piauí em foco",
    "piauiense pelo mundo vlog",
    "vida no piauí",

    # 3) 关键词组合
    "sotaque piauiense entrevista",
    "nordestino falando piauí",
    "oxe visse entrevistas",

    # 4) 节日 / 现场活动
    "festa junina piauí",
    "divino espirito santo piauí",

    # 5) 地方议会
    "assembleia legislativa piauí",
    "câmara municipal teresina sessão",
    "deputado piauiense falando",
]


# SE（Sergipe 塞尔希培，巴西东北部，州府阿拉卡茹 Aracaju）
# 目标语言现象：Sergipano，最小的东北州，Nordestino典型，"oxe/visse/crébeu" 等。
SE_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua aracaju",
    "entrevista rua sergipe",
    "povo de aracaju falando",
    "sergipano na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de aracaju",
    "sergipe em foco",
    "sergipano pelo mundo vlog",
    "vida em sergipe",

    # 3) 关键词组合
    "sotaque sergipano entrevista",
    "nordestino falando sergipe",
    "oxe crébeu entrevistas",

    # 4) 海滩文化 / 节日
    "carnaval sergipe entrevistas",
    "festa de são joão sergipe",

    # 5) 地方议会
    "assembleia legislativa sergipe",
    "câmara municipal aracaju sessão",
    "deputado sergipano falando",
]


# PB（Paraíba 帕拉伊巴，巴西东北部，州府若昂佩索阿 João Pessoa）
# 目标语言现象：Paraibano，东北方言，"oxe/visse/putz/rala" 等，元音-e 开口度大。
PB_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua joão pessoa",
    "entrevista rua paraíba",
    "povo de joão pessoa falando",
    "paraibano na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de joão pessoa",
    "paraíba em foco",
    "paraibano pelo mundo vlog",
    "vida na paraíba",

    # 3) 关键词组合
    "sotaque paraibano entrevista",
    "nordestino falando paraíba",
    "oxe putz entrevistas",

    # 4) 海岸文化 / 节日
    "carnaval paraíba entrevistas",
    "festa junina paraíba",
    "aréia paraíba entrevistas",

    # 5) 地方议会
    "assembleia legislativa paraíba",
    "câmara municipal joão pessoa sessão",
    "deputado paraibano falando",
]


# CE（Ceará 塞阿拉，巴西东北部，州府福塔莱萨 Fortaleza）
# 目标语言现象：Cearense，最具辨识度的东北方言之一，"oxe/visse/égua/oi" 等，口语夸张。
CE_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua fortaleza",
    "entrevista rua ceará",
    "povo de fortaleza falando",
    "cearense na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de fortaleza",
    "ceará em foco",
    "cearense pelo mundo vlog",
    "vida no ceará",

    # 3) 关键词组合
    "sotaque cearense entrevista",
    "nordestino falando ceará",
    "oxe égua entrevistas",
    "oi dim encurtou",

    # 4) 节日 / 海滩文化
    "carnaval fortaleza entrevistas",
    "festa junina ceará",
    "fortaleza beach entrevistas",

    # 5) 地方议会
    "assembleia legislativa ceará",
    "câmara municipal fortaleza sessão",
    "deputado cearense falando",
]


# BA（Bahia 巴伊亚，巴西东北部，州府萨尔瓦多 Salvador）
# 目标语言现象：Baiano，极具特色的东北方言，"oxe/visse/poxa/mané" 等，/r/ 发音独特，奴隶制历史影响。
BA_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua salvador",
    "entrevista rua bahia",
    "povo de salvador falando",
    "baiano na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de salvador",
    "bahia em foco",
    "baiano pelo mundo vlog",
    "vida na bahia",

    # 3) 关键词组合
    "sotaque baiano entrevista",
    "nordestino falando bahia",
    "oxe poxa entrevistas",
    "baiano falando axé",

    # 4) 非裔文化 / 节日
    "axé music entrevistas bahia",
    "carnaval salvador entrevistas",
    "festa de ipanerida bahia",
    "samba de roda entrevistas",

    # 5) 地方议会
    "assembleia legislativa bahia",
    "câmara municipal salvador sessão",
    "deputado baiano falando",
]


# ES（Espírito Santo 圣埃斯皮里图，巴西东南部，州府维多利亚 Vitória）
# 目标语言现象：Capixaba，东南沿海方言，受米纳斯和里约影响，但有独特词汇，"pira/dim/ih" 等。
ES_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua vitória",
    "entrevista rua espirito santo",
    "povo de vitória falando",
    "capixaba na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de vitória",
    "espirito santo em foco",
    "capixaba pelo mundo vlog",
    "vida no espírito santo",

    # 3) 关键词组合
    "sotaque capixaba entrevista",
    "fala capixaba natural",
    "capixaba falando pira",

    # 4) 沿海文化
    "praia espírito santo entrevistas",
    "carnaval capixaba",

    # 5) 地方议会
    "assembleia legislativa espírito santo",
    "câmara municipal vitória sessão",
    "deputado capixaba falando",
]


# RJ（Rio de Janeiro 里约热内卢，巴西东南部，州府里约市 Rio de Janeiro）
# 目标语言现象：Carioca，里约口音最具辨识度，"meu/N/çeu" 特征明显，/t/ /d/ 腭化，"vc/ tb" 缩写。
RJ_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua rio de janeiro",
    "entrevista no rio",
    "povo do rio falando",
    "carioca na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog do rio",
    "rio em foco",
    "carioca pelo mundo vlog",
    "vida no rio",

    # 3) 关键词组合
    "sotaque carioca entrevista",
    "fala carioca natural",
    "carioca falando meu",

    # 4) 贫民窟 / 街头文化
    "entrevista morro rio",
    "funk carioca entrevistas",
    "pagode rio entrevistas",

    # 5) 地方议会
    "assembleia legislativa rio de janeiro",
    "câmara municipal rio sessão",
    "deputado carioca falando",
]


# PE（Pernambuco 伯南布哥，巴西东北部，州府累西腓 Recife）
# 目标语言现象：Pernambucano/Recifense，Frevo/Maracutu 文化背景，"oxe/visse/rapá" 等。
PE_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua recife",
    "entrevista rua pernambuco",
    "povo de recife falando",
    "pernambucano na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de recife",
    "pernambuco em foco",
    "pernambucano pelo mundo vlog",
    "vida em pernambuco",

    # 3) 关键词组合
    "sotaque pernambucano entrevista",
    "nordestino falando pernambuco",
    "oxe rapá entrevistas",

    # 4) 文化特色
    "frevo recife entrevistas",
    "maracutu pernambuco entrevistas",
    "carnaval recife",
    "forró pernambuco",

    # 5) 地方议会
    "assembleia legislativa pernambuco",
    "câmara municipal recife sessão",
    "deputado pernambucano falando",
]


# MG（Minas Gerais 米纳斯吉拉斯，巴西东南部，州府贝洛奥里藏特 Belo Horizonte）
# 目标语言现象：Mineiro，大量缩减与连读（você→cê），"uai/trem/sô" 等辨识度高。
MG_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua belo horizonte",
    "entrevista rua minas gerais",
    "povo de belo horizonte falando",
    "mineiro na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de belo horizonte",
    "minas gerais em foco",
    "mineiro pelo mundo vlog",
    "vida em minas gerais",

    # 3) 关键词组合
    "sotaque mineiro entrevista",
    "fala mineira natural",
    "mineiro falando uai",
    "trem bão entrevista",

    # 4) 内陆城市
    "uberlândia entrevista rua",
    "juiz de fora falando",
    "montes claros entrevista",

    # 5) 地方议会
    "assembleia legislativa minas gerais",
    "câmara municipal belo horizonte sessão",
    "deputado mineiro falando",
]


# SP（São Paulo 圣保罗，巴西东南部，州府圣保罗市 São Paulo）
# 目标语言现象：Paulistano，口音相对中性但有特点，"vc/tô/né" 高频，人口最多元。
SP_DIALECT_QUERIES = [
    # 1) 街采类
    "entrevista rua são paulo",
    "entrevista no sp",
    "povo de são paulo falando",
    "paulistano na rua entrevista",

    # 2) 自媒体 / 地方 Vlog
    "vlog de são paulo",
    "sp em foco",
    "paulistano pelo mundo vlog",
    "vida em são paulo",

    # 3) 关键词组合
    "sotaque paulistano entrevista",
    "fala paulistana natural",
    "paulista falando tô",

    # 4) 多元文化
    "entrevista periferia sp",
    "cultura funk sp entrevistas",
    "pagode são paulo",

    # 5) 地方议会
    "assembleia legislativa são paulo",
    "câmara municipal são paulo sessão",
    "deputado paulistano falando",
]


# 把所有州方言检索词并入默认检索词表
DEFAULT_QUERIES = (
    RS_DIALECT_QUERIES + PR_DIALECT_QUERIES + GO_DIALECT_QUERIES +
    MS_DIALECT_QUERIES + RO_DIALECT_QUERIES + PA_DIALECT_QUERIES +
    PI_DIALECT_QUERIES + SE_DIALECT_QUERIES + PB_DIALECT_QUERIES +
    CE_DIALECT_QUERIES + BA_DIALECT_QUERIES + ES_DIALECT_QUERIES +
    RJ_DIALECT_QUERIES + PE_DIALECT_QUERIES + MG_DIALECT_QUERIES +
    SP_DIALECT_QUERIES
)

# 州缩写 -> 该州全部检索词（便于按地区筛选）
STATE_QUERIES: Dict[str, List[str]] = {
    "RS": RS_DIALECT_QUERIES,
    "PR": PR_DIALECT_QUERIES,
    "GO": GO_DIALECT_QUERIES,
    "MS": MS_DIALECT_QUERIES,
    "RO": RO_DIALECT_QUERIES,
    "PA": PA_DIALECT_QUERIES,
    "PI": PI_DIALECT_QUERIES,
    "SE": SE_DIALECT_QUERIES,
    "PB": PB_DIALECT_QUERIES,
    "CE": CE_DIALECT_QUERIES,
    "BA": BA_DIALECT_QUERIES,
    "ES": ES_DIALECT_QUERIES,
    "RJ": RJ_DIALECT_QUERIES,
    "PE": PE_DIALECT_QUERIES,
    "MG": MG_DIALECT_QUERIES,
    "SP": SP_DIALECT_QUERIES,
}

# ------------------------------------------------------------
# 口音区划分（与 docs/pt_dialect.md 对齐）
# ------------------------------------------------------------
# 5 类行政大区 geo_region_5
GEO_REGION_5: Dict[str, List[str]] = {
    "Norte": ["PA", "RO"],
    "Nordeste": ["BA", "PE", "CE", "AL", "PB", "PI", "SE"],
    "Sudeste": ["SP", "RJ", "MG", "ES"],
    "Sul": ["RS", "PR"],
    "Centro-Oeste": ["GO", "MS"],
}

# 7 类口音区 accent_region_7
ACCENT_REGION_7: Dict[str, List[str]] = {
    "Paulistano_Caipira": ["SP", "GO", "MS"],
    "Carioca": ["RJ"],
    "Mineiro": ["MG"],
    "Nordeste": ["BA", "PE", "CE", "AL", "PB", "PI", "SE"],
    "Norte": ["PA", "RO"],
    "Sul": ["RS", "PR"],
    "Sudeste_Transitional": ["ES"],
}

# 中文 / 别名 -> 规范标签
REGION_ALIASES: Dict[str, str] = {
    "东南": "Sudeste",
    "东南部": "Sudeste",
    "sudeste": "Sudeste",
    "southeast": "Sudeste",
    "北部": "Norte",
    "norte": "Norte",
    "东北": "Nordeste",
    "nordeste": "Nordeste",
    "南部": "Sul",
    "sul": "Sul",
    "中西部": "Centro-Oeste",
    "centro-oeste": "Centro-Oeste",
    "centrooeste": "Centro-Oeste",
    "圣保罗": "Paulistano_Caipira",
    "paulistano_caipira": "Paulistano_Caipira",
    "里约": "Carioca",
    "carioca": "Carioca",
    "米纳斯": "Mineiro",
    "mineiro": "Mineiro",
    "东南过渡": "Sudeste_Transitional",
    "sudeste_transitional": "Sudeste_Transitional",
}

# ============================================================
# 每个 query 额外附带两个标签：(state, category)
#   state    = 方言所属州缩写
#   category = 该 query 隶属的内容大类：
#                "street_interview" : 街采类
#                "local_media"      : 本地自媒体 / Vlog
#                "council"          : 地方议会 / 市政直播
# ------------------------------------------------------------
QUERY_TAGS: Dict[str, Dict[str, str]] = {
    # ---- RS (Rio Grande do Sul) ----
    "entrevista rua porto alegre":           {"state": "RS", "category": "street_interview"},
    "entrevista rua rio grande do sul":       {"state": "RS", "category": "street_interview"},
    "povo de poa falando":                   {"state": "RS", "category": "street_interview"},
    "gaúcho na rua entrevista":               {"state": "RS", "category": "street_interview"},
    "tchê entrevista":                        {"state": "RS", "category": "street_interview"},
    "vlog de porto alegre":                   {"state": "RS", "category": "local_media"},
    "gaúcho pelo mundo vlog":                {"state": "RS", "category": "local_media"},
    "tchê só canal":                          {"state": "RS", "category": "local_media"},
    "poa em foco":                           {"state": "RS", "category": "local_media"},
    "sotaque gaúcho entrevista":              {"state": "RS", "category": "street_interview"},
    "fala gaúcha natural":                   {"state": "RS", "category": "street_interview"},
    "assembleia legislativa rio grande do sul": {"state": "RS", "category": "council"},
    "câmara municipal porto alegre sessão":   {"state": "RS", "category": "council"},
    "deputado gaúcho falando":               {"state": "RS", "category": "council"},
    "vereador de poa falando":               {"state": "RS", "category": "council"},

    # ---- PR (Paraná) ----
    "entrevista rua curitiba":                {"state": "PR", "category": "street_interview"},
    "entrevista rua paraná":                 {"state": "PR", "category": "street_interview"},
    "povo de curitiba falando":               {"state": "PR", "category": "street_interview"},
    "paranaense na rua entrevista":           {"state": "PR", "category": "street_interview"},
    "vlog de curitiba":                       {"state": "PR", "category": "local_media"},
    "curitiba em 5 minutos":                  {"state": "PR", "category": "local_media"},
    "paranaense pelo mundo vlog":            {"state": "PR", "category": "local_media"},
    "sotaque paranaense entrevista":          {"state": "PR", "category": "street_interview"},
    "curitibano falando vixi":                {"state": "PR", "category": "street_interview"},
    "assembleia legislativa paraná":          {"state": "PR", "category": "council"},
    "câmara municipal curitiba sessão":      {"state": "PR", "category": "council"},
    "deputado paranaense falando":            {"state": "PR", "category": "council"},

    # ---- GO (Goiás) ----
    "entrevista rua goiânia":                {"state": "GO", "category": "street_interview"},
    "entrevista rua goiás":                  {"state": "GO", "category": "street_interview"},
    "povo de goiânia falando":                {"state": "GO", "category": "street_interview"},
    "goiano na rua entrevista":               {"state": "GO", "category": "street_interview"},
    "vlog de goiânia":                        {"state": "GO", "category": "local_media"},
    "goiânia em foco":                        {"state": "GO", "category": "local_media"},
    "goiano pelo mundo vlog":                {"state": "GO", "category": "local_media"},
    "sotaque goiano entrevista":              {"state": "GO", "category": "street_interview"},
    "fala goiana natural":                    {"state": "GO", "category": "street_interview"},
    "assembleia legislativa goiás":           {"state": "GO", "category": "council"},
    "câmara municipal goiânia sessão":       {"state": "GO", "category": "council"},
    "deputado goiano falando":               {"state": "GO", "category": "council"},

    # ---- MS (Mato Grosso do Sul) ----
    "entrevista rua campo grande":            {"state": "MS", "category": "street_interview"},
    "entrevista rua mato Grosso do sul":     {"state": "MS", "category": "street_interview"},
    "povo de campo grande falando":           {"state": "MS", "category": "street_interview"},
    "sul mato grossense na rua":              {"state": "MS", "category": "street_interview"},
    "vlog de campo grande":                   {"state": "MS", "category": "local_media"},
    "pantanal mato Grosso entrevistas":       {"state": "MS", "category": "local_media"},
    "sotaque sul mato grossense":            {"state": "MS", "category": "street_interview"},
    "fala pantaneira natural":               {"state": "MS", "category": "street_interview"},
    "assembleia legislativa mato Grosso do sul": {"state": "MS", "category": "council"},
    "câmara municipal campo grande sessão":  {"state": "MS", "category": "council"},
    "deputado sul mato grossense falando":   {"state": "MS", "category": "council"},

    # ---- RO (Rondônia) ----
    "entrevista rua porto velho":             {"state": "RO", "category": "street_interview"},
    "entrevista rua rondônia":               {"state": "RO", "category": "street_interview"},
    "povo de porto velho falando":            {"state": "RO", "category": "street_interview"},
    "rondoniano na rua entrevista":           {"state": "RO", "category": "street_interview"},
    "vlog de porto velho":                    {"state": "RO", "category": "local_media"},
    "rondônia em foco":                       {"state": "RO", "category": "local_media"},
    "sotaque rondoniano entrevista":          {"state": "RO", "category": "street_interview"},
    "fala amazônica natural":                {"state": "RO", "category": "street_interview"},
    "assembleia legislativa rondônia":        {"state": "RO", "category": "council"},
    "câmara municipal porto velho sessão":    {"state": "RO", "category": "council"},
    "deputado rondoniano falando":           {"state": "RO", "category": "council"},

    # ---- PA (Pará) ----
    "entrevista rua belém":                  {"state": "PA", "category": "street_interview"},
    "entrevista rua pará":                   {"state": "PA", "category": "street_interview"},
    "povo de belém falando":                  {"state": "PA", "category": "street_interview"},
    "paraense na rua entrevista":             {"state": "PA", "category": "street_interview"},
    "vlog de belém":                          {"state": "PA", "category": "local_media"},
    "belém em foco pará":                     {"state": "PA", "category": "local_media"},
    "paraense pelo mundo vlog":              {"state": "PA", "category": "local_media"},
    "sotaque paraense entrevista":            {"state": "PA", "category": "street_interview"},
    "fala amazônica pará":                   {"state": "PA", "category": "street_interview"},
    "assembleia legislativa pará":            {"state": "PA", "category": "council"},
    "câmara municipal belém sessão":          {"state": "PA", "category": "council"},
    "deputado paraense falando":             {"state": "PA", "category": "council"},

    # ---- PI (Piauí) ----
    "entrevista rua teresina":                {"state": "PI", "category": "street_interview"},
    "entrevista rua piauí":                  {"state": "PI", "category": "street_interview"},
    "povo de teresina falando":              {"state": "PI", "category": "street_interview"},
    "piauiense na rua entrevista":            {"state": "PI", "category": "street_interview"},
    "vlog de teresina":                       {"state": "PI", "category": "local_media"},
    "piauí em foco":                          {"state": "PI", "category": "local_media"},
    "piauiense pelo mundo vlog":             {"state": "PI", "category": "local_media"},
    "sotaque piauiense entrevista":           {"state": "PI", "category": "street_interview"},
    "nordestino falando piauí":              {"state": "PI", "category": "street_interview"},
    "assembleia legislativa piauí":           {"state": "PI", "category": "council"},
    "câmara municipal teresina sessão":      {"state": "PI", "category": "council"},
    "deputado piauiense falando":            {"state": "PI", "category": "council"},

    # ---- SE (Sergipe) ----
    "entrevista rua aracaju":                {"state": "SE", "category": "street_interview"},
    "entrevista rua sergipe":                {"state": "SE", "category": "street_interview"},
    "povo de aracaju falando":                {"state": "SE", "category": "street_interview"},
    "sergipano na rua entrevista":           {"state": "SE", "category": "street_interview"},
    "vlog de aracaju":                        {"state": "SE", "category": "local_media"},
    "sergipe em foco":                        {"state": "SE", "category": "local_media"},
    "sergipano pelo mundo vlog":            {"state": "SE", "category": "local_media"},
    "sotaque sergipano entrevista":          {"state": "SE", "category": "street_interview"},
    "nordestino falando sergipe":            {"state": "SE", "category": "street_interview"},
    "assembleia legislativa sergipe":         {"state": "SE", "category": "council"},
    "câmara municipal aracaju sessão":      {"state": "SE", "category": "council"},
    "deputado sergipano falando":            {"state": "SE", "category": "council"},

    # ---- PB (Paraíba) ----
    "entrevista rua joão pessoa":            {"state": "PB", "category": "street_interview"},
    "entrevista rua paraíba":                 {"state": "PB", "category": "street_interview"},
    "povo de joão pessoa falando":            {"state": "PB", "category": "street_interview"},
    "paraibano na rua entrevista":            {"state": "PB", "category": "street_interview"},
    "vlog de joão pessoa":                    {"state": "PB", "category": "local_media"},
    "paraíba em foco":                        {"state": "PB", "category": "local_media"},
    "paraibano pelo mundo vlog":             {"state": "PB", "category": "local_media"},
    "sotaque paraibano entrevista":           {"state": "PB", "category": "street_interview"},
    "nordestino falando paraíba":            {"state": "PB", "category": "street_interview"},
    "assembleia legislativa paraíba":         {"state": "PB", "category": "council"},
    "câmara municipal joão pessoa sessão":  {"state": "PB", "category": "council"},
    "deputado paraibano falando":            {"state": "PB", "category": "council"},

    # ---- CE (Ceará) ----
    "entrevista rua fortaleza":              {"state": "CE", "category": "street_interview"},
    "entrevista rua ceará":                  {"state": "CE", "category": "street_interview"},
    "povo de fortaleza falando":              {"state": "CE", "category": "street_interview"},
    "cearense na rua entrevista":             {"state": "CE", "category": "street_interview"},
    "vlog de fortaleza":                       {"state": "CE", "category": "local_media"},
    "ceará em foco":                          {"state": "CE", "category": "local_media"},
    "cearense pelo mundo vlog":              {"state": "CE", "category": "local_media"},
    "sotaque cearense entrevista":            {"state": "CE", "category": "street_interview"},
    "nordestino falando ceará":              {"state": "CE", "category": "street_interview"},
    "oxe égua entrevistas":                  {"state": "CE", "category": "street_interview"},
    "assembleia legislativa ceará":           {"state": "CE", "category": "council"},
    "câmara municipal fortaleza sessão":     {"state": "CE", "category": "council"},
    "deputado cearense falando":             {"state": "CE", "category": "council"},

    # ---- BA (Bahia) ----
    "entrevista rua salvador":               {"state": "BA", "category": "street_interview"},
    "entrevista rua bahia":                   {"state": "BA", "category": "street_interview"},
    "povo de salvador falando":               {"state": "BA", "category": "street_interview"},
    "baiano na rua entrevista":               {"state": "BA", "category": "street_interview"},
    "vlog de salvador":                       {"state": "BA", "category": "local_media"},
    "bahia em foco":                          {"state": "BA", "category": "local_media"},
    "baiano pelo mundo vlog":                {"state": "BA", "category": "local_media"},
    "sotaque baiano entrevista":              {"state": "BA", "category": "street_interview"},
    "nordestino falando bahia":              {"state": "BA", "category": "street_interview"},
    "baiano falando axé":                    {"state": "BA", "category": "street_interview"},
    "assembleia legislativa bahia":           {"state": "BA", "category": "council"},
    "câmara municipal salvador sessão":      {"state": "BA", "category": "council"},
    "deputado baiano falando":               {"state": "BA", "category": "council"},

    # ---- ES (Espírito Santo) ----
    "entrevista rua vitória":                {"state": "ES", "category": "street_interview"},
    "entrevista rua espirito santo":         {"state": "ES", "category": "street_interview"},
    "povo de vitória falando":                {"state": "ES", "category": "street_interview"},
    "capixaba na rua entrevista":             {"state": "ES", "category": "street_interview"},
    "vlog de vitória":                        {"state": "ES", "category": "local_media"},
    "espirito santo em foco":                 {"state": "ES", "category": "local_media"},
    "capixaba pelo mundo vlog":              {"state": "ES", "category": "local_media"},
    "sotaque capixaba entrevista":            {"state": "ES", "category": "street_interview"},
    "fala capixaba natural":                  {"state": "ES", "category": "street_interview"},
    "assembleia legislativa espírito santo": {"state": "ES", "category": "council"},
    "câmara municipal vitória sessão":       {"state": "ES", "category": "council"},
    "deputado capixaba falando":             {"state": "ES", "category": "council"},

    # ---- RJ (Rio de Janeiro) ----
    "entrevista rua rio de janeiro":          {"state": "RJ", "category": "street_interview"},
    "entrevista no rio":                     {"state": "RJ", "category": "street_interview"},
    "povo do rio falando":                    {"state": "RJ", "category": "street_interview"},
    "carioca na rua entrevista":              {"state": "RJ", "category": "street_interview"},
    "vlog do rio":                            {"state": "RJ", "category": "local_media"},
    "rio em foco":                            {"state": "RJ", "category": "local_media"},
    "carioca pelo mundo vlog":              {"state": "RJ", "category": "local_media"},
    "sotaque carioca entrevista":             {"state": "RJ", "category": "street_interview"},
    "fala carioca natural":                   {"state": "RJ", "category": "street_interview"},
    "entrevista morro rio":                   {"state": "RJ", "category": "street_interview"},
    "funk carioca entrevistas":               {"state": "RJ", "category": "local_media"},
    "pagode rio entrevistas":                 {"state": "RJ", "category": "local_media"},
    "assembleia legislativa rio de janeiro":  {"state": "RJ", "category": "council"},
    "câmara municipal rio sessão":          {"state": "RJ", "category": "council"},
    "deputado carioca falando":              {"state": "RJ", "category": "council"},

    # ---- PE (Pernambuco) ----
    "entrevista rua recife":                  {"state": "PE", "category": "street_interview"},
    "entrevista rua pernambuco":             {"state": "PE", "category": "street_interview"},
    "povo de recife falando":                 {"state": "PE", "category": "street_interview"},
    "pernambucano na rua entrevista":        {"state": "PE", "category": "street_interview"},
    "vlog de recife":                         {"state": "PE", "category": "local_media"},
    "pernambuco em foco":                     {"state": "PE", "category": "local_media"},
    "pernambucano pelo mundo vlog":         {"state": "PE", "category": "local_media"},
    "sotaque pernambucano entrevista":        {"state": "PE", "category": "street_interview"},
    "nordestino falando pernambuco":         {"state": "PE", "category": "street_interview"},
    "frevo recife entrevistas":               {"state": "PE", "category": "local_media"},
    "maracutu pernambuco entrevistas":        {"state": "PE", "category": "local_media"},
    "assembleia legislativa pernambuco":       {"state": "PE", "category": "council"},
    "câmara municipal recife sessão":        {"state": "PE", "category": "council"},
    "deputado pernambucano falando":         {"state": "PE", "category": "council"},

    # ---- MG (Minas Gerais) ----
    "entrevista rua belo horizonte":         {"state": "MG", "category": "street_interview"},
    "entrevista rua minas gerais":           {"state": "MG", "category": "street_interview"},
    "povo de belo horizonte falando":        {"state": "MG", "category": "street_interview"},
    "mineiro na rua entrevista":             {"state": "MG", "category": "street_interview"},
    "vlog de belo horizonte":                {"state": "MG", "category": "local_media"},
    "minas gerais em foco":                  {"state": "MG", "category": "local_media"},
    "mineiro pelo mundo vlog":               {"state": "MG", "category": "local_media"},
    "vida em minas gerais":                  {"state": "MG", "category": "local_media"},
    "sotaque mineiro entrevista":            {"state": "MG", "category": "street_interview"},
    "fala mineira natural":                  {"state": "MG", "category": "street_interview"},
    "mineiro falando uai":                   {"state": "MG", "category": "street_interview"},
    "trem bão entrevista":                   {"state": "MG", "category": "street_interview"},
    "uberlândia entrevista rua":             {"state": "MG", "category": "street_interview"},
    "juiz de fora falando":                  {"state": "MG", "category": "street_interview"},
    "montes claros entrevista":              {"state": "MG", "category": "street_interview"},
    "assembleia legislativa minas gerais":   {"state": "MG", "category": "council"},
    "câmara municipal belo horizonte sessão": {"state": "MG", "category": "council"},
    "deputado mineiro falando":              {"state": "MG", "category": "council"},

    # ---- SP (São Paulo) ----
    "entrevista rua são paulo":               {"state": "SP", "category": "street_interview"},
    "entrevista no sp":                      {"state": "SP", "category": "street_interview"},
    "povo de são paulo falando":              {"state": "SP", "category": "street_interview"},
    "paulistano na rua entrevista":           {"state": "SP", "category": "street_interview"},
    "vlog de são paulo":                       {"state": "SP", "category": "local_media"},
    "sp em foco":                              {"state": "SP", "category": "local_media"},
    "paulistano pelo mundo vlog":            {"state": "SP", "category": "local_media"},
    "sotaque paulistano entrevista":           {"state": "SP", "category": "street_interview"},
    "fala paulistana natural":                 {"state": "SP", "category": "street_interview"},
    "entrevista periferia sp":                 {"state": "SP", "category": "street_interview"},
    "cultura funk sp entrevistas":             {"state": "SP", "category": "local_media"},
    "assembleia legislativa são paulo":        {"state": "SP", "category": "council"},
    "câmara municipal são paulo sessão":     {"state": "SP", "category": "council"},
    "deputado paulistano falando":            {"state": "SP", "category": "council"},
}

# 追加检索词：用标题里真会出现的方言词、内陆城市街采、本地谈话来拉开和标准口音的距离。
# 原则：
# - 方言词（uai / tchê / oxe / gírias / sotaque / como o X fala）比「vlog + 州府」更能召回当地口语
# - 内陆城市口音通常比州府、比全国性频道更明显
# - papo / podcast / rádio 是长对话；曲风名（funk、pagode、frevo、axé）多半是唱歌，不适合 ASR
# category 仍用 street_interview / local_media / council，方便沿用现有 CSV 标记
EXTRA_DIALECT_QUERIES: Dict[str, List[Tuple[str, str]]] = {
    "RS": [
        ("gírias gaúchas", "street_interview"),
        ("mas bah tchê", "street_interview"),
        ("como o gaúcho fala", "street_interview"),
        ("sotaque de porto alegre", "street_interview"),
        ("entrevista de rua pelotas", "street_interview"),
        ("entrevista de rua caxias do sul", "street_interview"),
        ("entrevista de rua santa maria rs", "street_interview"),
        ("podcast gaúcho", "local_media"),
        ("papo gaúcho chimarrão", "local_media"),
    ],
    "PR": [
        ("gírias paranaenses", "street_interview"),
        ("sotaque curitibano", "street_interview"),
        ("como o paranaense fala", "street_interview"),
        ("piá paranaense falando", "street_interview"),
        ("entrevista de rua londrina", "street_interview"),
        ("entrevista de rua maringá", "street_interview"),
        ("entrevista de rua cascavel", "street_interview"),
        ("podcast curitiba", "local_media"),
    ],
    "GO": [
        ("gírias goianas", "street_interview"),
        ("sotaque goiano", "street_interview"),
        ("como o goiano fala", "street_interview"),
        ("uai goiano entrevista", "street_interview"),
        ("entrevista de rua anápolis", "street_interview"),
        ("entrevista de rua rio verde goiás", "street_interview"),
        ("podcast goiano", "local_media"),
    ],
    "MS": [
        ("gírias sul mato grossenses", "street_interview"),
        ("sotaque de campo grande", "street_interview"),
        ("como fala em mato grosso do sul", "street_interview"),
        ("entrevista de rua dourados", "street_interview"),
        ("entrevista de rua três lagoas", "street_interview"),
        ("podcast campo grande ms", "local_media"),
    ],
    "RO": [
        ("gírias de rondônia", "street_interview"),
        ("sotaque de porto velho", "street_interview"),
        ("como fala em rondônia", "street_interview"),
        ("entrevista de rua ji-paraná", "street_interview"),
        ("podcast porto velho", "local_media"),
    ],
    "PA": [
        ("gírias paraenses", "street_interview"),
        ("pai d'égua", "street_interview"),
        ("sotaque de belém", "street_interview"),
        ("como o paraense fala", "street_interview"),
        ("entrevista de rua santarém", "street_interview"),
        ("entrevista de rua marabá", "street_interview"),
        ("podcast paraense", "local_media"),
    ],
    "PI": [
        ("gírias piauienses", "street_interview"),
        ("sotaque de teresina", "street_interview"),
        ("oxe piauí entrevista", "street_interview"),
        ("como o piauiense fala", "street_interview"),
        ("entrevista de rua parnaíba", "street_interview"),
        ("podcast piauí", "local_media"),
    ],
    "SE": [
        ("gírias sergipanas", "street_interview"),
        ("sotaque de aracaju", "street_interview"),
        ("oxe sergipe entrevista", "street_interview"),
        ("como o sergipano fala", "street_interview"),
        ("podcast sergipe", "local_media"),
    ],
    "PB": [
        ("gírias paraibanas", "street_interview"),
        ("sotaque de joão pessoa", "street_interview"),
        ("oxe paraíba entrevista", "street_interview"),
        ("como o paraibano fala", "street_interview"),
        ("entrevista de rua campina grande", "street_interview"),
        ("podcast paraíba", "local_media"),
    ],
    "CE": [
        ("gírias cearenses", "street_interview"),
        ("sotaque de fortaleza", "street_interview"),
        ("oxe égua ceará", "street_interview"),
        ("como o cearense fala", "street_interview"),
        ("entrevista de rua juazeiro do norte", "street_interview"),
        ("entrevista de rua sobral", "street_interview"),
        ("podcast cearense", "local_media"),
    ],
    "BA": [
        ("gírias baianas", "street_interview"),
        ("sotaque baiano", "street_interview"),
        ("oxe bahia entrevista", "street_interview"),
        ("como o baiano fala", "street_interview"),
        ("meu rei entrevista salvador", "street_interview"),
        ("entrevista de rua feira de santana", "street_interview"),
        ("podcast baiano", "local_media"),
    ],
    "AL": [
        ("entrevista de rua maceió", "street_interview"),
        ("entrevista rua alagoas", "street_interview"),
        ("povo de maceió falando", "street_interview"),
        ("sotaque alagoano", "street_interview"),
        ("gírias alagoanas", "street_interview"),
        ("oxe alagoas entrevista", "street_interview"),
        ("como o alagoano fala", "street_interview"),
        ("podcast maceió", "local_media"),
        ("assembleia legislativa alagoas", "council"),
        ("câmara municipal maceió sessão", "council"),
    ],
    "ES": [
        ("gírias capixabas", "street_interview"),
        ("como o capixaba fala", "street_interview"),
        ("sotaque de vitória es", "street_interview"),
        ("entrevista de rua vila velha", "street_interview"),
        ("entrevista de rua serra espírito santo", "street_interview"),
        ("podcast capixaba", "local_media"),
        ("papo capixaba", "local_media"),
    ],
    "RJ": [
        ("gírias cariocas", "street_interview"),
        ("como o carioca fala", "street_interview"),
        ("sotaque do rio de janeiro", "street_interview"),
        ("tá ligado mano entrevista", "street_interview"),
        ("entrevista de rua niterói", "street_interview"),
        ("entrevista baixada fluminense", "street_interview"),
        ("entrevista nova iguaçu", "street_interview"),
        ("papo reto carioca", "local_media"),
        ("podcast carioca", "local_media"),
    ],
    "PE": [
        ("gírias pernambucanas", "street_interview"),
        ("sotaque de recife", "street_interview"),
        ("oxe pernambuco entrevista", "street_interview"),
        ("como o pernambucano fala", "street_interview"),
        ("entrevista de rua olinda", "street_interview"),
        ("entrevista de rua caruaru", "street_interview"),
        ("podcast pernambucano", "local_media"),
    ],
    "MG": [
        ("gírias mineiras", "street_interview"),
        ("mineirês", "street_interview"),
        ("como o mineiro fala", "street_interview"),
        ("uai sô", "street_interview"),
        ("cê vai uai", "street_interview"),
        ("sotaque do interior de minas", "street_interview"),
        ("entrevista de rua ouro preto", "street_interview"),
        ("entrevista de rua uberaba", "street_interview"),
        ("podcast mineiro", "local_media"),
        ("papo mineiro uai", "local_media"),
    ],
    "SP": [
        ("sotaque caipira", "street_interview"),
        ("gírias paulistas", "street_interview"),
        ("gírias da periferia de são paulo", "street_interview"),
        ("como o paulista fala", "street_interview"),
        ("ocê tá doido", "street_interview"),
        ("sotaque do interior de são paulo", "street_interview"),
        ("entrevista de rua campinas", "street_interview"),
        ("entrevista de rua ribeirão preto", "street_interview"),
        ("entrevista de rua sorocaba", "street_interview"),
        ("papo paulistano podcast", "local_media"),
    ],
}

EXTRA_QUERY_SET: set[str] = set()


def _register_extra_dialect_queries() -> None:
    """把方言特征词并入各州检索表，并补上 QUERY_TAGS。"""
    for state, items in EXTRA_DIALECT_QUERIES.items():
        bucket = STATE_QUERIES.setdefault(state, [])
        for query, category in items:
            if query not in bucket:
                bucket.append(query)
            QUERY_TAGS.setdefault(query, {"state": state, "category": category})
            EXTRA_QUERY_SET.add(query)


_register_extra_dialect_queries()

# 未在表里的 query 保持默认标签
# 粗粒度葡语/英语词表，用于弱规则判断。匹配按词边界，并忽略大小写。
PT_HINT_WORDS = {
    # ---- 巴西各州地名 ----
    # 州府和主要城市
    "porto", "alegre", "poa", "gaucho", "gaúcho", "sul", "sul brasil",
    "curitiba", "paraná", "paranaense", "curitibano", "pr",
    "goiânia", "goiania", "goiás", "goiano", "go",
    "campo", "grande", "pantanal", "sul-mato", "mato Grosso", "ms",
    "porto", "velho", "rondônia", "rondoniano", "ro",
    "belém", "belem", "pará", "paraense", "pa",
    "teresina", "piauí", "piauiense", "pi",
    "aracaju", "sergipe", "sergipano", "se",
    "joão", "pessoa", "paraíba", "paraibano", "pb",
    "fortaleza", "ceará", "cearense", "ce",
    "salvador", "bahia", "baiano", "ba",
    "vitória", "vitoria", "espírito", "espirito", "capixaba", "es",
    "rio", "janeiro", "carioca", "rj",
    "recife", "pernambuco", "pernambucano", "pe",
    "são", "paulo", "paulistano", "sp",

    # ---- 巴西各州方言特征词 ----
    # RS (Gaúcho)
    "tchê", "po", "azz", "bah", "canela", "farroupilha",

    # PR (Paranaense)
    "vixi", "rapá", "mané", "parana",

    # GO/GOIAS (Goiano)
    "uai", "né", "quer", "vixe",

    # MS (Sul-Mato-Grossense)
    "pantaneiro", "pantaneira", "boiadeiro",

    # RO (Rondoniano)
    "ribeirinho", "amazonas",

    # PA (Paraense)
    "mainha", "axe", "aí", "ué",

    # PI/NE (Nordestino 通用)
    "oxe", "oxente", "visse", "arretado", "crébeu", "putz", "rala",
    "égua", "egua", "dim", "poxa", "rapá", "mainha",
    "nordestino", "nordeste",

    # SE (Sergipano)
    "oxe", "cré", "crébeu",

    # PB (Paraibano)
    "putz", "rala", "oxe",

    # CE (Cearense) - 最具特色
    "oxe", "é", "égua", "oi", "dim", "visse",
    "cê", "vixe", "mainha",

    # BA (Baiano)
    "oxe", "poxa", "mané", "axé", "axé",

    # ES (Capixaba)
    "pira", "ih", "vixe",

    # RJ (Carioca)
    "meu", "vc", "tô", "ná", "moço", "moça", "çeu",

    # PE (Pernambucano)
    "oxe", "rapá", "frevo", "maracutu",

    # SP (Paulistano)
    "vc", "tô", "né", "pro", "pra", "mim",

    # ---- 文化相关词 ----
    "forró", "forro", "sanfona", "churrasco",
    "frevo", "maracutu", "axé", "funk", "pagode", "samba",
}
EN_HINT_WORDS = {
    "the", "and", "of", "to", "in", "for", "podcast", "news", "interview",
    "live", "english", "episode", "today",
}


def normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def count_word_hits(text: str, lexicon: Iterable[str]) -> int:
    """按词边界计数。短英文功能词不再误伤 fortaleza / minas 这类葡语词。"""
    hits = 0
    for w in lexicon:
        word = (w or "").lower()
        if not word:
            continue
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text):
            hits += 1
    return hits


_MUSIC_RELEASE_RE = re.compile(
    r"("
    r"clipe(\s+oficial)?|"
    r"official\s+(music\s+)?video|"
    r"lyric\s+video|"
    r"\blyrics\b|"
    r"\bkaraoke\b|"
    r"áudio\s+oficial|"
    r"audio\s+oficial|"
    r"\bvisualizer\b|"
    r"full\s+album|"
    r"álbum\s+completo|"
    r"album\s+completo"
    r")",
    re.IGNORECASE,
)


def looks_like_music_release(title: str) -> bool:
    """标题像歌曲成片（clipe / lyrics / karaoke），不是口语。"""
    return bool(_MUSIC_RELEASE_RE.search(title or ""))


def looks_like_english_only(title: str) -> bool:
    """标题里有至少两个英语功能词、且没有葡语线索。"""
    t = normalize_text(title)
    if not t:
        return False
    pt_hits = count_word_hits(t, PT_HINT_WORDS)
    en_hits = count_word_hits(t, EN_HINT_WORDS)
    return en_hits >= 2 and pt_hits == 0


def guess_language_and_codeswitch(title: str, desc: str) -> Tuple[str, str]:
    t = normalize_text(f"{title} {desc}")
    pt_hits = count_word_hits(t, PT_HINT_WORDS)
    en_hits = count_word_hits(t, EN_HINT_WORDS)

    if pt_hits == 0 and en_hits == 0:
        return "unknown", "待人工检查"
    if pt_hits > 0 and en_hits > 0:
        return "pt(+en?)", "是"
    if pt_hits > en_hits:
        return "pt", "否"
    if en_hits > pt_hits:
        return "en(+pt?)", "待人工检查"
    return "unknown", "待人工检查"


def classify_topic(text: str) -> str:
    lt = normalize_text(text or "")
    if any(
        k in lt
        for k in (
            "futebol",
            "football",
            "soccer",
            "brasileirão",
            "brasileirao",
            "copa",
            "seleção",
            "selecao",
            "champions",
            "estádio",
            "estadio",
        )
    ) or re.search(r"\b(nba|nfl)\b", lt):
        return "sports"
    if any(k in lt for k in ["notícia", "noticias", "news", "jornal"]):
        return "news"
    if any(k in lt for k in ["podcast", "entrevista", "interview", "debate"]):
        return "talk"
    if any(k in lt for k in ["aula", "curso", "lecture", "educação", "educacao"]):
        return "education"
    if any(k in lt for k in ["música", "musica", "song", "show", "concert"]):
        return "music"
    return "general"


def infer_channel_merge_key(entry: Dict[str, Any]) -> Tuple[str, str, str] | None:
    """
    从 yt-dlp 条目推断 (去重键, 频道主页 URL, 展示名)。
    键：优先 UC… channel_id；否则用规范化 @handle 或稳定 URL。
    """
    name = (
        (entry.get("channel") or entry.get("uploader") or entry.get("title") or "")
        or ""
    ).strip()

    # yt-dlp 在不同 extract/source 上字段名可能不同，所以从多个候选字段里抽取。
    candidates: List[str] = []
    for k in (
        "channel_id",
        "channel_url",
        "uploader_id",
        "uploader_url",
        "url",
        "webpage_url",
        "channel_handle",
    ):
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())

    # 1) 直接在候选文本中抽取 UC... channel id
    for v in candidates:
        m = re.search(r"(UC[\w-]{20,})", v)
        if m:
            cid = m.group(1)
            c_url = ""
            for k in ("channel_url", "uploader_url"):
                vv = entry.get(k)
                if isinstance(vv, str) and vv.strip():
                    c_url = vv.strip()
                    break
            if c_url:
                c_url = c_url.split("?")[0].rstrip("/")
                if c_url.endswith("/videos"):
                    c_url = c_url[: -len("/videos")]
            else:
                c_url = f"https://www.youtube.com/channel/{cid}"
            return cid, c_url, name

    # 2) 提取 @handle
    for v in candidates:
        m = re.search(r"youtube\.com/@([^/?#]+)", v)
        if not m:
            m = re.search(r"/@([^/?#]+)", v)
        if m:
            handle = m.group(1)
            if handle:
                c_url = f"https://www.youtube.com/@{handle}"
                return f"@{handle}", c_url, name or handle

    # 3) 尝试使用 channel_url/uploader_url 作为稳定 URL
    for k in ("channel_url", "uploader_url"):
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            u = v.strip().split("?")[0].rstrip("/")
            if u.endswith("/videos"):
                u = u[: -len("/videos")]
            if "youtube.com" in u and "/watch" not in u and "results?" not in u:
                return u, u, name

    # 4) 保底：仅用名称做去重键（避免写出 0 行）
    if name:
        merge = "name:" + re.sub(r"[^a-z0-9]+", "_", normalize_text(name))
        return merge, "", name

    return None


def parse_upload_date(s: str) -> dt.date | None:
    if not s:
        return None
    # yt-dlp 通常给 YYYYMMDD
    if re.fullmatch(r"\d{8}", s):
        return dt.datetime.strptime(s, "%Y%m%d").date()
    return None


# yt-dlp 运行时选项（由 main 注入：proxy / cookies）
_YTDLP_RUNTIME: Dict[str, Any] = {
    "proxy": "",
    "cookies": "",
}


def set_ytdlp_runtime(*, proxy: str = "", cookies: str = "") -> None:
    _YTDLP_RUNTIME["proxy"] = (proxy or "").strip()
    _YTDLP_RUNTIME["cookies"] = (cookies or "").strip()


def normalize_region_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    alias = REGION_ALIASES.get(s) or REGION_ALIASES.get(s.lower())
    if alias:
        return alias
    # 已是规范标签
    if s in GEO_REGION_5 or s in ACCENT_REGION_7:
        return s
    # 州码直接返回大写
    if len(s) == 2 and s.upper() in STATE_QUERIES:
        return s.upper()
    return s


def resolve_states_for_region(region: str, scheme: str) -> List[str]:
    """按 scheme 把地区标签解析为州码列表。"""
    r = normalize_region_name(region)
    if not r:
        return []
    # 直接传州码
    if r in STATE_QUERIES:
        return [r]
    table = GEO_REGION_5 if scheme == "geo5" else ACCENT_REGION_7
    if r not in table:
        known = sorted(set(list(GEO_REGION_5) + list(ACCENT_REGION_7) + list(STATE_QUERIES)))
        raise SystemExit(
            f"未知地区 {region!r}（规范化后 {r!r}）。"
            f"可用: {', '.join(known)}；或中文别名如「东南」。"
        )
    states = table[r]
    # 过滤掉尚无检索词的州
    available = [st for st in states if st in STATE_QUERIES]
    missing = [st for st in states if st not in STATE_QUERIES]
    if missing:
        print(f"[WARN] 地区 {r} 含暂无检索词的州 {missing}，已跳过这些州")
    if not available:
        raise SystemExit(f"地区 {r} 没有可用检索词的州")
    return available


def queries_for_states(states: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for st in states:
        for q in STATE_QUERIES.get(st, []):
            if q not in seen:
                seen.add(q)
                out.append(q)
    return out


def parse_state_labels(raw: str) -> List[str]:
    labels: List[str] = []
    for part in str(raw or "").split("+"):
        state = part.strip().upper()
        if state in STATE_QUERIES and state not in labels:
            labels.append(state)
    return labels


def states_to_dialect_regions(states: Iterable[str], scheme: str) -> str:
    """把州集合映射到口音区标签（+ 连接）。"""
    table = GEO_REGION_5 if scheme == "geo5" else ACCENT_REGION_7
    # 反向：state -> region
    inv: Dict[str, str] = {}
    for region, sts in table.items():
        for st in sts:
            inv[st] = region
    regions = sorted({inv[s] for s in states if s in inv})
    return "+".join(regions)


def format_state_and_geo5(states: Iterable[str]) -> Tuple[str, str]:
    """州标签，以及 docs/pt_dialect.md 的五类地区。"""
    ordered = sorted({s for s in states if s in STATE_QUERIES})
    return "+".join(ordered), states_to_dialect_regions(ordered, "geo5")


def _apply_runtime_opts(ydl_opts: Dict[str, Any]) -> Dict[str, Any]:
    proxy = _YTDLP_RUNTIME.get("proxy") or ""
    cookies = _YTDLP_RUNTIME.get("cookies") or ""
    if proxy:
        ydl_opts["proxy"] = proxy
    if cookies:
        if not Path_exists_safe(cookies):
            raise SystemExit(f"Cookies 文件不存在: {cookies}")
        ydl_opts["cookiefile"] = cookies
    return ydl_opts


def Path_exists_safe(p: str) -> bool:
    try:
        return os.path.isfile(p)
    except Exception:
        return False


def _node_path_for_ytdlp() -> str:
    return os.environ.get("YTDLP_NODE_PATH", r"C:\Program Files\nodejs\node.exe")


def _yt_dlp_extract_single(url: str) -> Dict[str, Any]:
    """
    抓取单个条目的更完整元数据，用于补齐搜索结果中缺失的频道字段。
    """
    node_path = _node_path_for_ytdlp()
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "ignoreerrors": True,
        "socket_timeout": 180,
        "retries": 3,
        "js_runtimes": {"node": {"path": node_path}},
        "remote_components": ["ejs:github"],
    }
    _apply_runtime_opts(ydl_opts)
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


def canonical_watch_url(url: str, video_id: str = "") -> str:
    text = (url or "").strip()
    match = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", text)
    vid = match.group(1) if match else (video_id or "").strip()
    if re.fullmatch(r"[\w-]{11}", vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return text.split("&")[0].strip()


def video_record_from_info(info: Dict[str, Any], source_type: str = "youtube_dialect") -> Dict[str, Any] | None:
    """整理成 crawl_all / run.sh 写入 Excel 的那一套字段。"""
    if not info:
        return None
    webpage_url = canonical_watch_url(
        info.get("webpage_url") or info.get("url") or "",
        str(info.get("id") or ""),
    )
    if "watch?v=" not in webpage_url and "youtu.be/" not in webpage_url:
        return None

    publish_time = ""
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts:
        try:
            publish_time = dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            publish_time = ""
    if not publish_time:
        ud = str(info.get("upload_date") or "")
        if re.fullmatch(r"\d{8}", ud):
            publish_time = f"{ud[:4]}-{ud[4:6]}-{ud[6:]}"

    categories = info.get("categories") or []
    if isinstance(categories, list):
        category = ", ".join(str(item) for item in categories if item)
    else:
        category = str(categories or "")
    if not category:
        category = str(info.get("category") or "")

    channel_name = (info.get("channel") or info.get("uploader") or "").strip()
    channel_url = info.get("channel_url") or ""
    uploader_id = str(info.get("uploader_id") or "")
    handle = ""
    if isinstance(channel_url, str):
        match = re.search(r"youtube\.com/@([^/?#]+)", channel_url, re.I)
        if match:
            handle = "@" + match.group(1)
    if not handle and uploader_id:
        if uploader_id.startswith("@"):
            handle = uploader_id
        elif not (uploader_id.startswith("UC") and len(uploader_id) == 24):
            handle = "@" + uploader_id.lstrip("@")

    duration = info.get("duration") or 0
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 0
    view_count = info.get("view_count") or 0
    try:
        view_count = int(view_count)
    except (TypeError, ValueError):
        view_count = 0

    return {
        "source_name": channel_name or handle,
        "source_type": source_type,
        "platform": "youtube",
        "title": info.get("title") or "",
        "url": webpage_url,
        "duration": duration,
        "category": category,
        "publish_time": publish_time,
        "view_count": view_count,
        "uploader": handle or channel_name,
    }


def metadata_passes_limits(
    video: Dict[str, Any],
    *,
    min_duration: int,
    max_duration: int,
    min_date: dt.date | None,
) -> bool:
    duration = int(video.get("duration") or 0)
    if duration and (duration < min_duration or duration > max_duration):
        return False
    if min_date and video.get("publish_time"):
        try:
            published = dt.datetime.strptime(str(video["publish_time"])[:10], "%Y-%m-%d").date()
        except ValueError:
            published = None
        if published and published < min_date:
            return False
    return True


class _MetadataLog:
    """元数据阶段不下载视频。YouTube 拒给播放流时的格式警告不影响标题/时长/播放量。"""

    _IGNORE = (
        "Video unavailable",
        "No video formats found",
        "Requested format is not available",
        "Only images are available",
    )

    def debug(self, msg: str) -> None:
        return

    def info(self, msg: str) -> None:
        return

    def warning(self, msg: str) -> None:
        text = str(msg)
        if any(snippet in text for snippet in self._IGNORE):
            return
        print(f"WARNING: {text}", file=sys.stderr)

    def error(self, msg: str) -> None:
        print(f"ERROR: {msg}", file=sys.stderr)


def _metadata_ydl_opts() -> Dict[str, Any]:
    node_path = _node_path_for_ytdlp()
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
        "ignoreerrors": True,
        "socket_timeout": 180,
        "retries": 3,
        "js_runtimes": {"node": {"path": node_path}},
        "remote_components": ["ejs:github"],
        "ignore_no_formats_error": True,
        "logger": _MetadataLog(),
        "extractor_args": {
            "youtube": {
                "skip": ["live", "members_only"],
            }
        },
    }
    # android 客户端不支持 cookies。带 cookie 时若仍指定它，yt-dlp 会跳过该客户端，
    # 只剩网页里的图片格式，于是每条视频都报 “Requested format is not available”。
    # 不指定 player_client，让 yt-dlp 按是否登录选择默认客户端。
    opts = _apply_runtime_opts(ydl_opts)
    if not opts.get("cookiefile"):
        opts["extractor_args"]["youtube"]["player_client"] = ["default"]
    return opts


def load_metadata_checkpoint(path: str) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    if not path or not os.path.isfile(path):
        return found
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = canonical_watch_url(str(rec.get("url") or ""))
            if url:
                rec["url"] = url
                found[url] = rec
    return found


def collect_video_metadata(
    urls: List[str],
    *,
    checkpoint_path: str,
    min_duration: int,
    max_duration: int,
    min_date: dt.date | None,
    state_by_url: Dict[str, set[str]] | None = None,
    channel_states: Dict[str, set[str]] | None = None,
) -> List[Dict[str, Any]]:
    """按 URL 拉完整元数据。checkpoint 里已有的 URL 不再请求。"""
    cached = load_metadata_checkpoint(checkpoint_path)
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = canonical_watch_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        ordered.append(url)

    kept: Dict[str, Dict[str, Any]] = {}
    pending: List[str] = []
    dropped_limits = 0
    for url in ordered:
        rec = cached.get(url)
        if not rec:
            pending.append(url)
            continue
        label_metadata_row(rec, state_by_url, channel_states)
        if metadata_passes_limits(
            rec,
            min_duration=min_duration,
            max_duration=max_duration,
            min_date=min_date,
        ):
            kept[url] = rec
        else:
            dropped_limits += 1

    print(
        f"[META] videos={len(ordered)} cached={len(ordered) - len(pending)} "
        f"to_fetch={len(pending)} checkpoint={checkpoint_path}"
    )
    os.makedirs(os.path.dirname(checkpoint_path) or ".", exist_ok=True)
    failed = 0
    if pending:
        with open(checkpoint_path, "a", encoding="utf-8") as sink, YoutubeDL(_metadata_ydl_opts()) as ydl:
            for index, url in enumerate(pending, 1):
                info: Dict[str, Any] = {}
                try:
                    info = ydl.extract_info(url, download=False) or {}
                except Exception as exc:
                    print(f"[META] {index}/{len(pending)} fail {url} -> {exc}")
                    failed += 1
                    continue
                rec = video_record_from_info(info)
                if not rec:
                    print(f"[META] {index}/{len(pending)} empty {url}")
                    failed += 1
                    continue
                label_metadata_row(rec, state_by_url, channel_states)
                sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
                sink.flush()
                if not metadata_passes_limits(
                    rec,
                    min_duration=min_duration,
                    max_duration=max_duration,
                    min_date=min_date,
                ):
                    dropped_limits += 1
                    print(f"[META] {index}/{len(pending)} filtered {rec.get('title') or url}")
                    continue
                kept[rec["url"]] = rec
                if index == 1 or index % 20 == 0 or index == len(pending):
                    print(f"[META] {index}/{len(pending)} ok {rec.get('title') or url}")

    print(f"[META] kept={len(kept)} failed={failed} filtered_by_limits={dropped_limits}")
    return [kept[url] for url in ordered if url in kept]


def _load_excel_utils():
    utils_path = os.path.join(os.path.dirname(__file__), "url_crawl", "utils.py")
    spec = importlib.util.spec_from_file_location("dialect_excel_utils", utils_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载 Excel 写入模块: {utils_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def label_metadata_row(
    video: Dict[str, Any],
    state_by_url: Dict[str, set[str]] | None = None,
    channel_states: Dict[str, set[str]] | None = None,
) -> Dict[str, Any]:
    """
    州：命中该视频的检索词所属州。没有视频级标签时，退回频道汇总表上的州。
    五类地区：始终按 geo5 从州映射，不随 --scheme 改变。
    """
    url = canonical_watch_url(str(video.get("url") or ""))
    states: set[str] = set(parse_state_labels(str(video.get("dialect_state") or "")))
    if state_by_url and url:
        states.update(state_by_url.get(url) or ())
    if not states and channel_states:
        for key in (
            video.get("source_name"),
            video.get("uploader"),
            str(video.get("uploader") or "").lstrip("@"),
        ):
            hit = channel_states.get((key or "").strip().lower())
            if hit:
                states.update(hit)
                break
    state_label, geo5_label = format_state_and_geo5(states)
    video["dialect_state"] = state_label
    video["dialect_region"] = geo5_label
    return video


def channel_state_index(channel_rows: Iterable[Dict[str, Any]]) -> Dict[str, set[str]]:
    index: Dict[str, set[str]] = {}
    for row in channel_rows:
        states = set(parse_state_labels(str(row.get("dialect_state") or "")))
        if not states:
            continue
        for key in (row.get("channel_name"), row.get("channel_handle")):
            text = (key or "").strip().lower()
            if text:
                index.setdefault(text, set()).update(states)
    return index


def save_metadata_excel(videos: List[Dict[str, Any]], output_path: str) -> None:
    """crawl_all 的列，并在末尾追加「州」「五类地区」。"""
    module = _load_excel_utils()
    headers = list(module.HEADERS) + ["州", "五类地区"]
    wb = module.openpyxl.Workbook()
    ws = wb.active
    ws.title = "视频列表"

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = module._HEADER_FONT
        cell.fill = module._HEADER_FILL
        cell.alignment = module._HEADER_ALIGN

    for row_idx, video in enumerate(videos, 2):
        duration_sec = video.get("duration", "")
        try:
            hours = round(float(duration_sec) / 3600.0, 3) if duration_sec != "" else ""
        except (TypeError, ValueError):
            hours = ""
        row_data = [
            video.get("url", ""),
            module.excel_channel_display_name(video),
            module.format_duration_hms(duration_sec),
            hours,
            video.get("category", ""),
            video.get("title", ""),
            video.get("source_name", ""),
            video.get("source_type", ""),
            video.get("platform", ""),
            video.get("publish_time", ""),
            video.get("view_count", ""),
            video.get("dialect_state", ""),
            video.get("dialect_region", ""),
        ]
        for col, val in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.alignment = module._CENTER_ALIGN

    preferred_widths = [60, 28, 15, 15, 35, 75, 50, 30, 15, 30, 12, 16, 22]
    for index, width in enumerate(preferred_widths, 1):
        ws.column_dimensions[module.openpyxl.utils.get_column_letter(index)].width = width

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    wb.save(output_path)


# 检索词里的虚词和体裁词。匹配看地名、方言词、节目名，不看「entrevista / rua / vlog」。
_QUERY_STOPWORDS = frozenset({
    "a", "o", "as", "os", "um", "uma", "uns", "umas",
    "de", "da", "do", "das", "dos", "em", "na", "no", "nas", "nos",
    "e", "que", "para", "pra", "por", "com", "se", "ao", "aos", "ou",
    "rua", "entrevista", "entrevistas", "falando", "fala", "vlog",
    "video", "natural", "povo", "vida", "foco", "sessao", "municipal",
    "camara", "assembleia", "legislativa", "deputado", "vereador",
    "canal", "pelo", "mundo", "como", "mais", "muito", "esse", "essa",
    "isso", "aqui", "the", "and", "of", "to", "in", "for", "on",
})
_STATE_CODE_TOKENS = frozenset({
    "ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg",
    "ms", "mt", "pa", "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr",
    "rs", "sc", "se", "sp", "to",
})
# --max-results 0 时的保险丝，避免 YouTube 一直返回满页时无限翻页。
_ADAPT_SAFETY_CAP = 2000


def fold_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def query_strong_tokens(query: str) -> List[str]:
    """去掉虚词后的关键词。短州码（sp、mg）保留，其余至少 3 个字母。"""
    folded = fold_accents(normalize_text(query))
    tokens = re.findall(r"[a-z0-9']+", folded)
    strong: List[str] = []
    for token in tokens:
        if token in _QUERY_STOPWORDS:
            continue
        if len(token) < 3 and token not in _STATE_CODE_TOKENS:
            continue
        if token not in strong:
            strong.append(token)
    return strong


def entry_search_text(entry: Dict[str, Any]) -> str:
    parts = [
        entry.get("title"),
        entry.get("channel"),
        entry.get("uploader"),
        entry.get("description"),
    ]
    return fold_accents(normalize_text(" ".join(str(p) for p in parts if p)))


def keyword_match_ratio(entry: Dict[str, Any], strong_tokens: List[str]) -> float:
    """
    命中的实词比例。没有实词时视为匹配（无从筛选）。
    检索词里若有长度 >= 5 的实词，至少要命中其中一个，避免只蹭到「sao」这种短词。
    """
    if not strong_tokens:
        return 1.0
    text = entry_search_text(entry)
    hits = [
        token for token in strong_tokens
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text)
    ]
    if not hits:
        return 0.0
    long_tokens = [token for token in strong_tokens if len(token) >= 5]
    if long_tokens and not any(token in hits for token in long_tokens):
        return 0.0
    return len(hits) / len(strong_tokens)


def entry_matches_keywords(
    entry: Dict[str, Any],
    strong_tokens: List[str],
    min_ratio: float,
) -> bool:
    return keyword_match_ratio(entry, strong_tokens) + 1e-9 >= min_ratio


def _page_hit_rates(flags: List[bool], tail_size: int) -> Tuple[float, float]:
    if not flags:
        return 0.0, 0.0
    hit_rate = sum(1 for flag in flags if flag) / len(flags)
    tail = flags[-min(tail_size, len(flags)):]
    tail_rate = sum(1 for flag in tail if flag) / len(tail)
    return hit_rate, tail_rate


def _should_expand_page(
    hit_rate: float,
    tail_rate: float,
    *,
    full_page: bool,
    room_left: bool,
    hit_min: float,
    tail_min: float,
) -> bool:
    """满页，且（整页命中率高，或页尾仍匹配）时继续翻。"""
    if not full_page or not room_left:
        return False
    return hit_rate >= hit_min or tail_rate >= tail_min


def _yt_dlp_extract_entries(
    label: str,
    extract_url: str,
    *,
    playlist_start: int,
    playlist_end: int,
) -> List[Dict[str, Any]]:
    """
    拉取搜索结果的一个区间（1-based，含首尾）。
    yt-dlp 的 continuation 必须从第 1 条走到 playlist_end，后面的页会重走前面的页。
    """
    node_path = _node_path_for_ytdlp()
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 180,
        "retries": 5,
        "playliststart": playlist_start,
        "playlistend": playlist_end,
        "js_runtimes": {"node": {"path": node_path}},
        "remote_components": ["ejs:github"],
    }
    _apply_runtime_opts(ydl_opts)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"[INFO] {label} attempt={attempt}/{max_attempts}")
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(extract_url, download=False)

            if not info:
                print(f"[WARN] empty info for {label}")
                return []

            entries = info.get("entries", []) or []
            print(f"[INFO] {label} got {len(entries)} entries")
            return [e for e in entries if e]

        except Exception as e:
            print(f"[WARN] {label} failed attempt={attempt}/{max_attempts} -> {e}")
            if attempt == max_attempts:
                print("[ERROR] final failure details:")
                print(f"  - extract_url: {extract_url}")
                print(f"  - node_path   : {node_path}")
                print(f"  - proxy      : {_YTDLP_RUNTIME.get('proxy') or '(none)'}")
                print(f"  - hint       : verify node path + yt-dlp EJS settings + proxy")
                print(traceback.format_exc())
                return []
            time.sleep(2 ** (attempt - 1))

    return []


# 视频搜索排序。相关性之外再按上传日期翻一页长尾；
# 按播放量排序容易冒出全国性热门（歌曲、标准口音），默认不启用。
# sp 是 YouTube 搜索过滤器 protobuf：sort + type=video。
# relevance 用 yt-dlp 内置的「仅视频」参数，避免和 date/views 的 sp 重复编码。
_VIDEO_RELEVANCE_SP = "EgIQAfABAQ=="
VIDEO_SEARCH_SORTS: Dict[str, str] = {
    "relevance": _VIDEO_RELEVANCE_SP,
    "date": "CAISAhAB",
    "views": "CAMSAhAB",
}
_CHANNEL_TAB_SP = "EgIQAg=="


def _search_results_url(query: str, sp: str) -> str:
    return (
        "https://www.youtube.com/results?"
        f"search_query={urllib.parse.quote(query)}&sp={urllib.parse.quote(sp)}"
    )


def _fetch_result_window(
    label: str,
    query: str,
    sp: str,
    start: int,
    end: int,
) -> List[Dict[str, Any]]:
    """取第 start..end 条（1-based）。若 yt-dlp 仍返回从头开始的列表，只留这一窗。"""
    entries = _yt_dlp_extract_entries(
        f"{label} [{start}-{end}]",
        _search_results_url(query, sp),
        playlist_start=start,
        playlist_end=end,
    )
    want = end - start + 1
    if len(entries) > want:
        entries = entries[-want:]
    return entries


def adaptive_search(
    *,
    label: str,
    query: str,
    sp: str,
    page_size: int,
    max_results: int,
    tail_size: int,
    hit_min: float,
    tail_min: float,
    min_ratio: float,
) -> List[Dict[str, Any]]:
    """
    先取一页，丢掉关键词匹配度低于 min_ratio 的条目。
    整页命中率 >= hit_min，或页尾命中率 >= tail_min，且本页是满的，则再取下一页。
    后面的页用同一条规则，避免第一页很高之后把后几百条噪声也收进来。
    """
    strong = query_strong_tokens(query)
    cap = max_results if max_results > 0 else _ADAPT_SAFETY_CAP
    kept: List[Dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    page = 0
    while offset < cap:
        page += 1
        start = offset + 1
        end = min(offset + page_size, cap)
        window = end - start + 1
        entries = _fetch_result_window(label, query, sp, start, end)
        if not entries:
            print(f"[ADAPT] {label} page={page} empty -> stop")
            break
        flags = [entry_matches_keywords(entry, strong, min_ratio) for entry in entries]
        hit_rate, tail_rate = _page_hit_rates(flags, tail_size)
        matched_n = sum(1 for flag in flags if flag)
        full_page = len(entries) >= window
        room_left = end < cap
        expand = _should_expand_page(
            hit_rate,
            tail_rate,
            full_page=full_page,
            room_left=room_left,
            hit_min=hit_min,
            tail_min=tail_min,
        )
        decision = "expand" if expand else "stop"
        if full_page and room_left and not expand:
            decision = "stop (match fell off)"
        elif not full_page:
            decision = "stop (short page)"
        elif not room_left:
            decision = "stop (cap)"
        print(
            f"[ADAPT] {label} page={page} fetched={len(entries)} "
            f"matched={matched_n} hit={hit_rate:.2f} tail={tail_rate:.2f} "
            f"tokens={strong or ['(none)']} -> {decision}"
        )
        for entry, ok in zip(entries, flags):
            if not ok:
                continue
            video_id = (entry.get("id") or "").strip()
            if video_id:
                if video_id in seen:
                    continue
                seen.add(video_id)
            kept.append(entry)
        if not expand:
            break
        offset = end
    return kept


def fetch_search_results(
    query: str,
    sorts: List[str],
    *,
    page_size: int,
    max_results: int,
    tail_size: int,
    hit_min: float,
    tail_min: float,
    min_ratio: float,
) -> List[Dict[str, Any]]:
    """每种视频排序各自自适应翻页，再按视频 id 去重。"""
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for sort in sorts:
        if sort not in VIDEO_SEARCH_SORTS:
            print(f"[WARN] unknown search sort {sort!r}, skipped")
            continue
        entries = adaptive_search(
            label=f"video sort={sort} query={query!r}",
            query=query,
            sp=VIDEO_SEARCH_SORTS[sort],
            page_size=page_size,
            max_results=max_results,
            tail_size=tail_size,
            hit_min=hit_min,
            tail_min=tail_min,
            min_ratio=min_ratio,
        )
        for entry in entries:
            video_id = (entry.get("id") or "").strip()
            if video_id:
                if video_id in seen:
                    continue
                seen.add(video_id)
            merged.append(entry)
    return merged


def fetch_channel_tab_search_results(
    query: str,
    *,
    page_size: int,
    max_results: int,
    tail_size: int,
    hit_min: float,
    tail_min: float,
    min_ratio: float,
) -> List[Dict[str, Any]]:
    """「频道」筛选页，同一套翻页和关键词筛选。"""
    return adaptive_search(
        label=f"channel-tab query={query!r}",
        query=query,
        sp=_CHANNEL_TAB_SP,
        page_size=page_size,
        max_results=max_results,
        tail_size=tail_size,
        hit_min=hit_min,
        tail_min=tail_min,
        min_ratio=min_ratio,
    )


def main() -> None:
    _configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        description=(
            "按巴西口音区发现 YouTube 频道（默认东南 Sudeste）。"
            "划分见 docs/pt_dialect.md。"
        )
    )
    parser.add_argument(
        "--output",
        default="",
        help="频道汇总 CSV 路径（默认 results/dialect_<region>_channels.csv）",
    )
    parser.add_argument(
        "--metadata-out",
        default="",
        help="视频元数据 Excel，列与 run.sh / crawl_all 相同"
             "（默认 results/dialect_<region>_metadata.xlsx）",
    )
    parser.add_argument(
        "--skip-metadata",
        action="store_true",
        help="只发现视频 URL，不拉取时长、播放量、发布时间等元数据",
    )
    parser.add_argument(
        "--region",
        default="Sudeste",
        help="口音区/大区名。geo5: Norte/Nordeste/Sudeste/Sul/Centro-Oeste；"
             "accent7: Paulistano_Caipira/Carioca/Mineiro/...；"
             "也可用中文「东南」或州码 SP。默认 Sudeste",
    )
    parser.add_argument(
        "--scheme",
        choices=("geo5", "accent7"),
        default="geo5",
        help="地区划分方案：geo5=5 类行政大区（默认），accent7=7 类口音区",
    )
    parser.add_argument(
        "--states",
        nargs="*",
        default=[],
        help="直接指定州码（如 SP RJ MG ES），若给出则忽略 --region",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="可选：传给 yt-dlp 的 HTTP 代理，例如 http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--cookies",
        default="",
        help="YouTube cookies.txt（可选，默认尝试 config/youtube_cookies.txt）",
    )
    parser.add_argument(
        "--max-per-query",
        type=int,
        default=100,
        help="每个 query、每种排序的起始页大小。默认 100。命中率仍高时会继续翻页",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=500,
        help="每个 query、每种排序的结果上限。0 表示只按匹配率停（内部保险丝 2000）。默认 500",
    )
    parser.add_argument(
        "--hit-rate",
        type=float,
        default=0.45,
        help="整页关键词命中率达到该值则继续翻页。默认 0.45",
    )
    parser.add_argument(
        "--tail-rate",
        type=float,
        default=0.30,
        help="本页末尾的关键词命中率达到该值则继续翻页。默认 0.30",
    )
    parser.add_argument(
        "--tail-size",
        type=int,
        default=20,
        help="用本页最后 N 条计算页尾命中率。默认 20",
    )
    parser.add_argument(
        "--min-keyword-ratio",
        type=float,
        default=0.5,
        help="标题/频道名至少覆盖检索词实词的比例，低于此值丢弃。默认 0.5",
    )
    parser.add_argument(
        "--search-sorts",
        default="relevance,date",
        help="视频搜索排序，逗号分隔：relevance,date,views。"
             "每种排序各自按命中率翻页。默认 relevance,date",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=0,
        help="视频侧发布日期窗口（天）。0 表示不限制（默认）",
    )
    parser.add_argument(
        "--keep-music",
        action="store_true",
        help="保留标题像歌曲成片的视频（默认丢弃 clipe / lyrics / karaoke）",
    )
    parser.add_argument(
        "--keep-english",
        action="store_true",
        help="保留标题像纯英语、且没有葡语线索的视频（默认丢弃）",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=20,
        help="视频侧：最短时长（秒），用于统计命中",
    )
    parser.add_argument("--max-duration", type=int, default=4 * 3600, help="视频侧：最长时长（秒）")
    parser.add_argument(
        "--queries",
        nargs="*",
        default=None,
        help="自定义检索词。不传则按 --region/--states 自动选取",
    )
    parser.add_argument(
        "--video-urls-out",
        default="",
        help="额外写出命中的视频 watch URL 列表（供后续下载音频）",
    )
    parser.add_argument(
        "--channels-tsv-out",
        default="",
        help="额外写出 crawl_all 可用的 TSV：name\\tyoutube_channel\\turl",
    )
    parser.add_argument(
        "--skip-channel-tab",
        action="store_true",
        help="不抓取「频道」筛选结果页，仅用视频搜索聚合频道",
    )
    parser.add_argument(
        "--no-hydrate-channel-info",
        action="store_true",
        help="关闭兜底：当搜索条目缺失 channel 字段时，对 entry.url 再抽取一次元数据（更稳但更慢）。",
    )
    parser.add_argument(
        "--list-regions",
        action="store_true",
        help="列出可用地区后退出",
    )
    args = parser.parse_args()

    if args.list_regions:
        print("=== geo5 (docs/pt_dialect.md §1.2) ===")
        for k, v in GEO_REGION_5.items():
            print(f"  {k}: {', '.join(v)}")
        print("=== accent7 (docs/pt_dialect.md §1.1) ===")
        for k, v in ACCENT_REGION_7.items():
            print(f"  {k}: {', '.join(v)}")
        print("=== 州码（有检索词）===")
        print(" ", ", ".join(sorted(STATE_QUERIES)))
        return

    # 解析目标州
    if args.states:
        states = [s.upper() for s in args.states]
        bad = [s for s in states if s not in STATE_QUERIES]
        if bad:
            raise SystemExit(f"未知或暂无检索词的州: {bad}；可用: {sorted(STATE_QUERIES)}")
        region_label = "+".join(states)
    else:
        states = resolve_states_for_region(args.region, args.scheme)
        region_label = normalize_region_name(args.region)

    if args.queries is not None and len(args.queries) > 0:
        queries = list(args.queries)
    else:
        queries = queries_for_states(states)

    # 输出路径
    out_csv = args.output.strip()
    if not out_csv:
        safe = re.sub(r"[^\w\-]+", "_", region_label)
        out_csv = os.path.join("results", f"dialect_{safe}_channels.csv")
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)

    # 代理 / cookies
    proxy = (args.proxy or "").strip()
    cookies = (args.cookies or "").strip()
    if not cookies:
        default_ck = os.path.join("config", "youtube_cookies.txt")
        if os.path.isfile(default_ck):
            cookies = default_ck
    set_ytdlp_runtime(proxy=proxy, cookies=cookies)

    sorts = [s.strip().lower() for s in (args.search_sorts or "").split(",") if s.strip()]
    bad_sorts = [s for s in sorts if s not in VIDEO_SEARCH_SORTS]
    if bad_sorts:
        raise SystemExit(
            f"未知 --search-sorts: {bad_sorts}；可选: {', '.join(VIDEO_SEARCH_SORTS)}"
        )
    if not sorts:
        raise SystemExit("至少指定一种 --search-sorts，例如 relevance,date")

    if args.max_per_query < 1:
        raise SystemExit("--max-per-query 至少为 1")
    if args.tail_size < 1:
        raise SystemExit("--tail-size 至少为 1")
    if not 0.0 <= args.hit_rate <= 1.0 or not 0.0 <= args.tail_rate <= 1.0:
        raise SystemExit("--hit-rate 和 --tail-rate 需在 0 到 1 之间")
    if not 0.0 <= args.min_keyword_ratio <= 1.0:
        raise SystemExit("--min-keyword-ratio 需在 0 到 1 之间")

    extra_n = sum(1 for q in queries if q in EXTRA_QUERY_SET)
    days_label = "unlimited" if args.days_back <= 0 else str(args.days_back)
    cap_label = "until-match-drops" if args.max_results <= 0 else str(args.max_results)
    print(f"[CONFIG] region={region_label} scheme={args.scheme} states={states}")
    print(
        f"[CONFIG] queries={len(queries)} (dialect_markers={extra_n}) "
        f"page_size={args.max_per_query} max_results={cap_label} "
        f"hit_rate>={args.hit_rate:.2f} or tail_rate>={args.tail_rate:.2f} "
        f"min_keyword_ratio={args.min_keyword_ratio:.2f} "
        f"video_sorts={','.join(sorts)}"
    )
    safe_region = re.sub(r"[^\w\-]+", "_", region_label)
    metadata_xlsx = (args.metadata_out or "").strip()
    if not metadata_xlsx:
        metadata_xlsx = os.path.join(
            os.path.dirname(out_csv) or "results",
            f"dialect_{safe_region}_metadata.xlsx",
        )
    metadata_ckpt = os.path.splitext(metadata_xlsx)[0] + ".partial.jsonl"

    print(f"[CONFIG] days_back={days_label}")
    print(f"[CONFIG] proxy={proxy or '(none)'} cookies={cookies or '(none)'}")
    print(f"[CONFIG] output={out_csv}")
    print(
        f"[CONFIG] metadata={'(skipped)' if args.skip_metadata else metadata_xlsx}"
    )

    today = dt.date.today()
    min_date = today - dt.timedelta(days=args.days_back) if args.days_back > 0 else None
    hydrate_enabled = not args.no_hydrate_channel_info

    channels: Dict[str, Dict[str, Any]] = {}
    video_urls: List[str] = []
    seen_video_urls: set[str] = set()
    video_states: Dict[str, set[str]] = {}

    def remember_video_state(url: str, query: str) -> None:
        canon = canonical_watch_url(url)
        if not canon:
            return
        state, _ = tag_for_query(query)
        if state in STATE_QUERIES:
            video_states.setdefault(canon, set()).add(state)

    def ensure_ch(ch_key: str) -> Dict[str, Any]:
        if ch_key not in channels:
            channels[ch_key] = {
                "merge_key": ch_key,
                "channel_url": "",
                "channel_name": "",
                "matched_pt_video_count": 0,
                "queries": set(),
                "states": set(),
                "categories": set(),
                "sample_titles": [],
                "from_channel_tab": False,
                "from_video_search": False,
            }
        return channels[ch_key]

    def tag_for_query(q: str) -> Tuple[str, str]:
        tag = QUERY_TAGS.get(q)
        if tag:
            return tag.get("state", "other"), tag.get("category", "other")
        return "other", "other"

    # --- 频道标签页搜索 ---
    if not args.skip_channel_tab:
        for q in queries:
            try:
                entries = fetch_channel_tab_search_results(
                    q,
                    page_size=args.max_per_query,
                    max_results=args.max_results,
                    tail_size=args.tail_size,
                    hit_min=args.hit_rate,
                    tail_min=args.tail_rate,
                    min_ratio=args.min_keyword_ratio,
                )
            except Exception as e:
                print(f"[WARN] channel-tab query failed: {q} -> {e}")
                continue
            for e in entries:
                parsed = infer_channel_merge_key(e)
                if parsed:
                    ch_key, ch_url, name = parsed
                else:
                    name = (
                        (e.get("channel") or e.get("uploader") or e.get("title") or "")
                        or ""
                    ).strip()
                    if not name and hydrate_enabled:
                        probe_url = (e.get("url") or e.get("webpage_url") or "").strip()
                        if probe_url:
                            hydrated = _yt_dlp_extract_single(probe_url)
                            parsed2 = infer_channel_merge_key(hydrated)
                            if parsed2:
                                ch_key, ch_url, name = parsed2
                                c = ensure_ch(ch_key)
                                c["from_channel_tab"] = True
                                c["queries"].add(q)
                                st_q, cat_q = tag_for_query(q)
                                c["states"].add(st_q)
                                if cat_q != "other":
                                    c["categories"].add(cat_q)
                                if ch_url:
                                    c["channel_url"] = ch_url
                                if name:
                                    c["channel_name"] = name
                                continue
                            name = (
                                (hydrated.get("uploader") or hydrated.get("channel") or hydrated.get("title") or "")
                                or ""
                            ).strip()
                    if not name:
                        continue
                    ch_key = "name:" + re.sub(r"[^a-z0-9]+", "_", normalize_text(name))
                    ch_url = ""
                c = ensure_ch(ch_key)
                c["from_channel_tab"] = True
                c["queries"].add(q)
                st_q, cat_q = tag_for_query(q)
                c["states"].add(st_q)
                if cat_q != "other":
                    c["categories"].add(cat_q)
                if ch_url:
                    c["channel_url"] = ch_url
                if name:
                    c["channel_name"] = name

    # --- 视频搜索聚合频道 ---
    seen_video: set[str] = set()
    dropped_music = 0
    dropped_english = 0
    for q in queries:
        try:
            entries = fetch_search_results(
                q,
                sorts,
                page_size=args.max_per_query,
                max_results=args.max_results,
                tail_size=args.tail_size,
                hit_min=args.hit_rate,
                tail_min=args.tail_rate,
                min_ratio=args.min_keyword_ratio,
            )
        except Exception as e:
            print(f"[WARN] video query failed: {q} -> {e}")
            continue

        for e in entries:
            video_id = e.get("id")
            if not video_id:
                continue
            watch_now = (e.get("url") or e.get("webpage_url") or "").strip()
            if not watch_now:
                watch_now = f"https://www.youtube.com/watch?v={video_id}"
            if video_id in seen_video:
                remember_video_state(watch_now, q)
                continue

            duration_val = e.get("duration")
            duration = int(duration_val) if duration_val else 0
            upload_date = parse_upload_date(e.get("upload_date") or "")
            if duration and (duration < args.min_duration or duration > args.max_duration):
                continue
            if min_date and upload_date and upload_date < min_date:
                continue

            title_for_filter = (e.get("title") or "").strip()
            if not args.keep_music and looks_like_music_release(title_for_filter):
                dropped_music += 1
                seen_video.add(video_id)
                continue
            if not args.keep_english and looks_like_english_only(title_for_filter):
                dropped_english += 1
                seen_video.add(video_id)
                continue

            watch = watch_now
            remember_video_state(watch, q)
            if watch and watch not in seen_video_urls:
                seen_video_urls.add(watch)
                video_urls.append(watch)

            parsed = infer_channel_merge_key(e)
            if parsed:
                ch_key, ch_url, _ = parsed
            else:
                ch_key = ""
                ch_url = ""
                name = (
                    (e.get("channel") or e.get("uploader") or e.get("title") or "")
                    or ""
                ).strip()
                if not name and hydrate_enabled:
                    probe_url = (e.get("url") or e.get("webpage_url") or "").strip()
                    if probe_url:
                        hydrated = _yt_dlp_extract_single(probe_url)
                        parsed2 = infer_channel_merge_key(hydrated)
                        if parsed2:
                            ch_key, ch_url, name2 = parsed2
                            name = name2 or name
                        else:
                            name = (
                                (hydrated.get("uploader") or hydrated.get("channel") or hydrated.get("title") or "")
                                or ""
                            ).strip()
                if not ch_key:
                    if not name:
                        continue
                    ch_key = "name:" + re.sub(r"[^a-z0-9]+", "_", normalize_text(name))
                    ch_url = ""

            seen_video.add(video_id)
            title = (e.get("title", "") or "").strip()
            uploader = (e.get("uploader", "") or "").strip()

            c = ensure_ch(ch_key)
            c["from_video_search"] = True
            c["queries"].add(q)
            st_q, cat_q = tag_for_query(q)
            c["states"].add(st_q)
            if cat_q != "other":
                c["categories"].add(cat_q)
            c["matched_pt_video_count"] = int(c["matched_pt_video_count"]) + 1
            if ch_url:
                c["channel_url"] = ch_url
            if uploader:
                c["channel_name"] = uploader
            if title:
                st: List[str] = c["sample_titles"]
                if title not in st and len(st) < 8:
                    st.append(title)

    rows: List[Dict[str, Any]] = []
    for ch_key, c in channels.items():
        channel_id = ch_key if ch_key.startswith("UC") and len(ch_key) >= 22 else ""
        channel_handle = ch_key[1:] if ch_key.startswith("@") else ""
        name = c["channel_name"]
        samples_join = " | ".join(c["sample_titles"][:5])
        lang_hint, code_switch = guess_language_and_codeswitch(
            f"{name} {samples_join}", ""
        )
        topic = classify_topic(f"{name} {samples_join}")
        for t in c["sample_titles"][:5]:
            tp = classify_topic(t)
            if tp != "general":
                topic = tp
                break

        src: List[str] = []
        if c["from_channel_tab"]:
            src.append("channel_tab")
        if c["from_video_search"]:
            src.append("video_search")
        discovery = "+".join(src) if src else "unknown"

        states_sorted = sorted(c["states"])
        is_multi_state = any("+" in s for s in states_sorted)
        dialect_state = "+".join(states_sorted) if states_sorted else ""
        dialect_region = states_to_dialect_regions(
            [s for s in states_sorted if s in STATE_QUERIES],
            args.scheme,
        )

        cats = c["categories"]
        is_street = "yes" if "street_interview" in cats else "no"
        is_media = "yes" if "local_media" in cats else "no"
        is_council = "yes" if "council" in cats else "no"

        rows.append(
            {
                "channel_id": channel_id,
                "channel_handle": channel_handle,
                "channel_name": name,
                "channel_url": c["channel_url"],
                "matched_pt_video_count": c["matched_pt_video_count"],
                "queries": ";".join(sorted(c["queries"])),
                "discovery_source": discovery,
                "lang_hint": lang_hint,
                "code_switch": code_switch,
                "topic": topic,
                "target_region": region_label,
                "dialect_scheme": args.scheme,
                "dialect_state": dialect_state,
                "dialect_region": dialect_region,
                "is_multi_state": "yes" if is_multi_state else "no",
                "is_street_interview": is_street,
                "is_local_media": is_media,
                "is_council": is_council,
                "sample_titles": samples_join,
                "source_platform": "youtube",
                "metadata_only": "yes",
                "note": "",
            }
        )

    rows.sort(
        key=lambda x: (
            -int(x["matched_pt_video_count"] or 0),
            x["channel_name"] or "",
            x["channel_url"] or "",
        )
    )

    fieldnames = [
        "channel_id",
        "channel_handle",
        "channel_name",
        "channel_url",
        "matched_pt_video_count",
        "queries",
        "discovery_source",
        "lang_hint",
        "code_switch",
        "topic",
        "target_region",
        "dialect_scheme",
        "dialect_state",
        "dialect_region",
        "is_multi_state",
        "is_street_interview",
        "is_local_media",
        "is_council",
        "sample_titles",
        "source_platform",
        "metadata_only",
        "note",
    ]

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(
        f"[FILTER] dropped music_titles={dropped_music} english_titles={dropped_english}"
    )
    print(f"[DONE] wrote {len(rows)} channels -> {out_csv}")

    export_urls = list(video_urls)
    if not args.skip_metadata:
        metadata_rows = collect_video_metadata(
            video_urls,
            checkpoint_path=metadata_ckpt,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
            min_date=min_date,
            state_by_url=video_states,
            channel_states=channel_state_index(rows),
        )
        save_metadata_excel(metadata_rows, metadata_xlsx)
        export_urls = [row["url"] for row in metadata_rows if row.get("url")]
        print(f"[DONE] wrote {len(metadata_rows)} video metadata rows -> {metadata_xlsx}")

    if args.video_urls_out:
        os.makedirs(os.path.dirname(args.video_urls_out) or ".", exist_ok=True)
        with open(args.video_urls_out, "w", encoding="utf-8") as f:
            for u in export_urls:
                f.write(u + "\n")
        print(f"[DONE] wrote {len(export_urls)} video URLs -> {args.video_urls_out}")

    if args.channels_tsv_out:
        os.makedirs(os.path.dirname(args.channels_tsv_out) or ".", exist_ok=True)
        n = 0
        with open(args.channels_tsv_out, "w", encoding="utf-8") as f:
            for r in rows:
                url = (r.get("channel_url") or "").strip()
                name = (r.get("channel_name") or "unknown").strip() or "unknown"
                if not url:
                    continue
                # crawl_all 输入格式：name \\t type \\t id
                f.write(f"{name}\tyoutube_channel\t{url}\n")
                n += 1
        print(f"[DONE] wrote {n} channel TSV rows -> {args.channels_tsv_out}")

    print("视频元数据 Excel 与 run.sh 的列一致，可直接交给后续下载或 merge_metadata_xlsx.py。")


if __name__ == "__main__":
    main()

