const cnOption = (symbol, ticker, label, extra = {}) => ({
  symbol,
  ticker,
  label,
  market: 'CN',
  realtime: true,
  priceLabel: '点位',
  pricePrecision: 2,
  ...extra,
});

export const US_ETF_OPTIONS = [
  { symbol: 'SOXX.US', ticker: 'SOXX', label: '半导体', market: 'US' },
  { symbol: 'SPY.US', ticker: 'SPY', label: '标普500', market: 'US' },
  { symbol: 'QQQ.US', ticker: 'QQQ', label: '纳指100', market: 'US' },
  { symbol: 'DIA.US', ticker: 'DIA', label: '道琼斯', market: 'US' },
  { symbol: 'GLD.US', ticker: 'GLD', label: '黄金', market: 'US', realtime: false },
];

export const HK_ETF_OPTIONS = [
  { symbol: 'HSI.HK', ticker: '恒生指数', label: '港股', market: 'HK', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: 'HSTECH.HK', ticker: '恒生科技', label: '港股', market: 'HK', realtime: false, priceLabel: '点位', pricePrecision: 2 },
];

export const CN_GENERAL_GROUPS = [
  {
    key: 'broad-and-style',
    title: '宽基及风格指数',
    options: [
      cnOption('000300.SH', '沪深300', '宽基'),
      cnOption('000985.SH', '中证全指', '宽基'),
      cnOption('899050.BJ', '北证50', '宽基'),
      cnOption('INNO100.CN', 'A创100', '创新100'),
      cnOption('000510.SH', '中证A500', '宽基'),
      cnOption('000905.SH', '中证500', '宽基'),
      cnOption('000680.SH', '科创综指', '宽基'),
      cnOption('000688.SH', '科创50', '宽基'),
      cnOption('000698.SH', '科创100', '宽基'),
      cnOption('000699.SH', '科创200', '宽基'),
      cnOption('399006.SZ', '创业板指', '宽基'),
      cnOption('000015.SH', '上证红利', '风格'),
      cnOption('H30269.CSI', '红利低波', '风格'),
    ],
  },
];

export const CN_INDUSTRY_GROUPS = [
  {
    key: 'energy',
    title: '能源',
    taxonomyCode: '880300',
    layout: 'wide',
    children: [
      { title: '煤炭', code: '880301', options: [cnOption('399998.SZ', '中证煤炭', '二级行业')] },
      { title: '电力', code: '880305', options: [cnOption('H30199.CSI', '电力公用', '二级行业'), cnOption('931151.CSI', '光伏产业', '三级行业', { leafLabel: '新型电力' })] },
      { title: '电网设备', options: [cnOption('931994.CSI', '电网设备', '二级行业')] },
      { title: '石油石化', code: '880310', options: [cnOption('H30198.CSI', '油气产业', '二级行业')] },
    ],
  },
  {
    key: 'materials',
    title: '原材料',
    taxonomyCode: '880320',
    options: [cnOption('000987.SH', '全指原材料', '一级行业')],
    children: [
      { title: '钢铁', code: '880321', options: [cnOption('930606.CSI', '中证钢铁', '二级行业')] },
      { title: '有色', code: '880325', options: [cnOption('000819.SH', '有色金属', '二级行业'), cnOption('930598.CSI', '稀土产业', '三级行业', { leafLabel: '小金属' })] },
      { title: '化工', code: '880332', options: [cnOption('000813.CSI', '细分化工', '二级行业')] },
      { title: '建材', code: '880340', options: [cnOption('931009.CSI', '建筑材料', '二级行业')] },
    ],
  },
  {
    key: 'industrials',
    title: '工业制造',
    taxonomyCode: '880360',
    layout: 'wide',
    children: [
      { title: '工程机械', code: '880447', options: [cnOption('931752.CSI', '工程机械', '二级行业')] },
      { title: '基建', options: [cnOption('399995.SZ', '基建工程', '二级行业')] },
      { title: '专用设备', code: '880445', options: [cnOption('980022.SZ', '机器人产业', '二级行业')] },
      { title: '汽车整车', code: '880361', options: [cnOption('930997.CSI', '新能源车', '二级行业')] },
      { title: '汽车零部件', code: '880365', options: [cnOption('931230.CSI', '汽车零部件', '三级行业')] },
      { title: '运输物流', code: '880370', options: [cnOption('H30171.CSI', '全指运输', '二级行业')] },
    ],
  },
  {
    key: 'discretionary',
    title: '可选消费',
    taxonomyCode: '880390',
    layout: 'wide',
    options: [cnOption('000989.SH', '全指可选消费', '一级行业')],
    children: [
      { title: '家电', code: '880391', options: [cnOption('980028.SZ', '龙头家电', '二级行业')] },
      { title: '文化传媒', code: '880408', options: [cnOption('399971.SZ', '中证传媒', '二级行业'), cnOption('930901.CSI', '动漫游戏', '三级行业')] },
      { title: '旅游酒店', code: '880414', options: [cnOption('930633.CSI', '中证旅游', '二级行业')] },
    ],
  },
  {
    key: 'staples',
    title: '主要消费',
    taxonomyCode: '880420',
    options: [cnOption('000932.SH', '主要消费', '一级行业')],
    children: [
      { title: '饮料', code: '880428', options: [cnOption('399997.SZ', '中证白酒', '三级行业', { leafLabel: '白酒' })] },
      { title: '农林牧渔', code: '880438', options: [cnOption('000949.CSI', '中证农业', '二级行业'), cnOption('930707.CSI', '畜牧养殖', '三级行业')] },
    ],
  },
  {
    key: 'healthcare',
    title: '医药卫生',
    taxonomyCode: '880400',
    options: [cnOption('000991.SH', '全指医药', '一级行业')],
    children: [
      { title: '综合医药', options: [cnOption('000933.SH', '医药卫生', '二级行业')] },
      { title: '化学制药', code: '880401', options: [cnOption('931152.CSI', '创新药', '三级行业')] },
      { title: '中药', code: '880405', options: [cnOption('930641.CSI', '中证中药', '二级行业')] },
      { title: '生物制品', code: '880411', options: [cnOption('930726.CSI', '生物医药', '二级行业')] },
      { title: '医疗器械', code: '880417', options: [cnOption('H30217.CSI', '医疗器械', '二级行业')] },
      { title: '医疗服务', code: '880480', options: [cnOption('399989.SZ', '中证医疗', '二级行业')] },
    ],
  },
  {
    key: 'financials',
    title: '金融',
    taxonomyCode: '880990',
    children: [
      { title: '银行', code: '880471', options: [cnOption('399986.SZ', '中证银行', '二级行业')] },
      { title: '证券', code: '880472', options: [cnOption('399975.SZ', '证券公司', '二级行业')] },
      { title: '保险', code: '880473', options: [cnOption('930618.CSI', '中证保险', '二级行业')] },
      { title: '证券保险', options: [cnOption('H30588.CSI', '中证证保', '二级行业')] },
    ],
  },
  {
    key: 'technology',
    title: '信息技术',
    taxonomyCode: '880992',
    options: [cnOption('000993.SH', '全指信息', '一级行业')],
    children: [
      {
        title: '半导体',
        code: '880491',
        options: [
          cnOption('H30184.CSI', '半导体', '二级行业'),
          cnOption('950162.CSI', '芯片设计', '三级行业'),
          cnOption('931743.CSI', '材料与设备', '三级行业'),
        ],
      },
      { title: '通信设备', code: '880490', options: [cnOption('931160.CSI', '通信设备', '二级行业')] },
      { title: '通信服务', options: [cnOption('000994.CSI', '全指通信服务', '二级行业')] },
      { title: '电脑设备', code: '880489', options: [cnOption('930651.CSI', '中证计算机', '二级行业')] },
      { title: '软件服务', code: '880493', options: [cnOption('H30202.CSI', '全指软件', '二级行业'), cnOption('930851.CSI', '云计算', '三级行业')] },
    ],
  },
  {
    key: 'utilities',
    title: '公用事业',
    taxonomyCode: '880451',
    options: [cnOption('000995.CSI', '全指公用', '一级行业')],
    children: [],
  },
  {
    key: 'real-estate',
    title: '房地产',
    taxonomyCode: '880460',
    options: [cnOption('931775.CSI', '房地产', '一级行业')],
    children: [],
  },
  {
    key: 'defense',
    title: '国防军工',
    taxonomyCode: '880478',
    options: [cnOption('399967.SZ', '中证军工', '一级行业')],
    children: [],
  },
];

export const CN_ETF_OPTIONS = [
  ...CN_GENERAL_GROUPS.flatMap(group => group.options),
  ...CN_INDUSTRY_GROUPS.flatMap(group => [
    ...(group.options || []),
    ...(group.children || []).flatMap(child => child.options || []),
  ]),
];

export const ETF_OPTIONS = [...US_ETF_OPTIONS, ...HK_ETF_OPTIONS, ...CN_ETF_OPTIONS];
