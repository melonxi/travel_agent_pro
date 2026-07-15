# backend/harness/airport_groups.py
"""机场别名分组数据。

从 validator 中拆出，便于扩展到日本以外的目的地。key 是机场组标识，
value 是识别该机场的别名集合（中英文/IATA 码，全部小写匹配）。
新增目的地时在此追加即可，无需改校验逻辑。
"""

from __future__ import annotations

AIRPORT_GROUPS: dict[str, set[str]] = {
    # 日本
    "haneda": {"羽田", "haneda", "hnd"},
    "narita": {"成田", "narita", "nrt"},
    "kansai": {"关西", "関西", "kansai", "kix"},
    "itami": {"伊丹", "itami", "itm"},
    "chubu": {"中部", "名古屋", "chubu", "centrair", "ngo"},
    "fukuoka": {"福冈", "福岡", "fukuoka", "fuk"},
    "new_chitose": {"新千岁", "新千歳", "札幌", "chitose", "sapporo", "cts"},
    "naha": {"那霸", "那覇", "冲绳", "沖縄", "naha", "okinawa", "oka"},
    # 中国大陆主要枢纽
    "beijing_capital": {"首都", "北京首都", "capital", "pek"},
    "beijing_daxing": {"大兴", "北京大兴", "daxing", "pkx"},
    "shanghai_pudong": {"浦东", "上海浦东", "pudong", "pvg"},
    "shanghai_hongqiao": {"虹桥", "上海虹桥", "hongqiao", "sha"},
    "guangzhou": {"白云", "广州", "baiyun", "can"},
    "shenzhen": {"宝安", "深圳", "baoan", "szx"},
    "chengdu_tianfu": {"天府", "成都天府", "tianfu", "tfu"},
    "chengdu_shuangliu": {"双流", "成都双流", "shuangliu", "ctu"},
    # 港澳台
    "hongkong": {"香港", "hong kong", "hongkong", "hkg"},
    "macau": {"澳门", "澳門", "macau", "mfm"},
    "taoyuan": {"桃园", "桃園", "台北桃园", "taoyuan", "tpe"},
    "songshan": {"松山", "台北松山", "songshan", "tsa"},
    # 东南亚常见目的地
    "bangkok_suvarnabhumi": {"素万那普", "曼谷", "suvarnabhumi", "bkk"},
    "bangkok_donmuang": {"廊曼", "don mueang", "donmuang", "dmk"},
    "singapore": {"樟宜", "新加坡", "changi", "singapore", "sin"},
    "kuala_lumpur": {"吉隆坡", "kuala lumpur", "klia", "kul"},
    "bali": {"巴厘", "巴厘岛", "denpasar", "bali", "dps"},
    "seoul_incheon": {"仁川", "首尔仁川", "incheon", "icn"},
    "seoul_gimpo": {"金浦", "首尔金浦", "gimpo", "gmp"},
}
