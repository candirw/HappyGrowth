/* ============================================================
 * inventory.example.js —— 示例库存（开箱即用）
 *
 * 这是给新用户看的样板：一套够用的「家庭吧台起步配置」。
 * 想改成自己的库存：
 *   1. 复制本文件为 inventory.js
 *   2. 只改 inventory.js（它已在 .gitignore 里，不会被提交）
 *
 * 命名约定：name 统一写成「品类-品牌」，例如「威士忌-尊美醇」。
 * 这样在下拉列表和库存芯片里一眼能看出是什么酒。
 *
 * 字段说明见 docs/customize-inventory.md
 * ============================================================ */

window.BAR_CONFIG = {

  /* 本地存储前缀：改这里可以和别人的数据区分开，一般不用动 */
  storagePrefix: 'cj',

  /* ---------- 酒瓶 ----------
   * code   编号，自定义，短一点好记
   * name   显示名称，「品类-品牌」格式
   * covers 这瓶酒能满足配方里的哪些「用料名」，可写多个
   *        —— 这是「能否调配」的判断依据，写错会影响可做判定
   */
  spirits: [
    {
      code: 'S01', name: '伏特加-绝对', covers: ['伏特加'],
      type: '伏特加', abv: '40%', origin: '瑞典',
      taste: '中性、干净，几乎没有杂味。做配方时用来测比例最合适，因为它不抢戏。',
      keep: '常温避光直立，开瓶后 1–2 年风味基本稳定', warn: false,
      use: '螺丝起子、莫斯科骡子、伏特加苏打、大都会'
    },
    {
      code: 'S02', name: '白朗姆-百加得', covers: ['白朗姆'],
      type: '白朗姆', abv: '40%', origin: '波多黎各',
      taste: '清爽、干净，带极轻的甘蔗甜。白朗姆里的标准款，做酸甜类最通用。',
      keep: '常温避光直立，开瓶后 1–2 年风味基本稳定', warn: false,
      use: '莫吉托、自由古巴、黛克瑞'
    },
    {
      code: 'S03', name: '金酒-孟买蓝宝石', covers: ['金酒'],
      type: '伦敦干金酒', abv: '47%', origin: '英国',
      taste: '十种植物配方，柑橘和花香突出，杜松子偏柔和，香气比较奔放。',
      keep: '常温避光直立，开瓶后 1–2 年风味基本稳定', warn: false,
      use: '金汤力、金酒苏打、干马天尼'
    },
    {
      code: 'S04', name: '威士忌-尊美醇', covers: ['威士忌', '爱尔兰威士忌'],
      type: '爱尔兰威士忌', abv: '40%', origin: '爱尔兰',
      taste: '三次蒸馏，顺滑，带香草和淡淡果香；没有苏格兰那种烟熏味，好入口。',
      keep: '常温避光直立，开瓶后 1–2 年风味基本稳定', warn: false,
      use: '威士忌酸、威士忌苏打、教父、爱尔兰咖啡'
    },
    {
      code: 'S05', name: '龙舌兰-奥美佳', covers: ['龙舌兰'],
      type: '龙舌兰（混合型）', abv: '38%', origin: '墨西哥',
      taste: '柔和偏甜，植物气息不算强，被果汁盖住也不可惜，适合做果味混饮。',
      keep: '常温避光直立，开瓶后 1–2 年风味基本稳定', warn: false,
      use: '龙舌兰日出、龙舌兰苏打、玛格丽特'
    },
    {
      code: 'S06', name: '白兰地-必得利', covers: ['白兰地'],
      type: '白兰地 VSOP', abv: '40%', origin: '法国',
      taste: '果香为主，带橡木和一点香草。做酸甜类比威士忌更柔，纯饮常温也够格。',
      keep: '常温避光直立，开瓶后 1–2 年风味基本稳定', warn: false,
      use: '白兰地酸、白兰地橙汁、纯饮'
    },
    {
      code: 'L01', name: '橙味利口酒-君度', covers: ['君度'],
      type: '橙味利口酒 triple sec', abv: '40%', origin: '法国',
      taste: '橙皮香气干净清亮，甜但不腻，是橙味利口酒里的基准款。',
      keep: '含糖利口酒，开瓶后 6–12 个月内用完；常温避光直立', warn: true,
      use: '玛格丽特、大都会、B52 顶层'
    },
    {
      code: 'L02', name: '咖啡利口酒-卡鲁瓦', covers: ['卡鲁瓦'],
      type: '咖啡利口酒', abv: '20%', origin: '墨西哥',
      taste: '朗姆基底，咖啡与焦糖，甜度较高但苦味撑得住，做咖啡类调酒的骨架。',
      keep: '含糖利口酒，开瓶后 6–12 个月内用完；常温避光直立', warn: true,
      use: 'B52 垫底、白俄罗斯、黑俄罗斯、Espresso Martini'
    },
    {
      code: 'L03', name: '奶油利口酒-百利', covers: ['百利'],
      type: '奶油利口酒', abv: '17%', origin: '爱尔兰',
      taste: '威士忌加奶油和可可，甜、厚、像液体甜点。遇酸会结块，别和柠檬青柠混。',
      keep: '⚠ 开瓶后必须冷藏，6 个月内喝完，超期会分层变质', warn: true,
      use: 'B52 中层、Mudslide、百利冰咖啡'
    },
    {
      code: 'L04', name: '杏仁利口酒-帝萨诺', covers: ['帝萨诺'],
      type: '杏仁利口酒', abv: '28%', origin: '意大利',
      taste: '杏仁甜香，带一点樱桃感和香草，甜度高，一滴就能盖住威士忌。',
      keep: '含糖利口酒，开瓶后 6–12 个月内用完；常温避光直立', warn: true,
      use: '教父（威士忌 + 帝萨诺 25ml）'
    },
    {
      code: 'L05', name: '苦味利口酒-金巴利', covers: ['金巴利'],
      type: '苦味利口酒 bitter', abv: '20.5–25%', origin: '意大利米兰',
      taste: '苦橙皮、龙胆草和草本香料，苦味清晰还带甜。少数把「苦」当主角的利口酒。',
      keep: '含糖利口酒，开瓶后 6–12 个月内用完；常温避光直立', warn: true,
      use: '内格罗尼、Americano、加里波第'
    }
  ],

  /* ---------- 辅料 ----------
   * key      内部键名，英文短词，不要重复
   * name     显示名称
   * covers   对应配方里的用料名，一般和 name 一致或写别名
   * def      初始状态：have 有 / none 没有 / unknown 待确认
   *          用户可以在页面里点芯片随时改，改完存本地
   * optional 为 true 时，缺了不影响「可做」判定（例如蛋白）
   */
  supplies: [
    { key: 'lime',      name: '青柠',   covers: ['青柠汁'], hint: '现榨，一个出汁 25–30ml', def: 'have' },
    { key: 'lemon',     name: '柠檬',   covers: ['柠檬汁'], hint: '现榨', def: 'have' },
    { key: 'syrup',     name: '糖浆',   covers: ['糖浆'],   hint: '原味糖浆，或白砂糖 1:1 热水化开自制', def: 'have' },
    { key: 'sugar',     name: '白砂糖', covers: ['白砂糖'], hint: '厨房常备，也可自制糖浆', def: 'have' },
    { key: 'salt',      name: '盐',     covers: ['盐'],     hint: '杯边用', def: 'have' },
    { key: 'ice',       name: '冰',     covers: ['冰'],     hint: '', def: 'have' },
    { key: 'soda',      name: '苏打水', covers: ['苏打水'], hint: '无糖无味，别和汤力水混淆', def: 'have' },
    { key: 'orange',    name: '橙汁',   covers: ['橙汁'],   hint: '100% 非浓缩还原，开封冷藏 3 天内用完', def: 'have' },
    { key: 'tonic',     name: '汤力水', covers: ['汤力水'], hint: '不能用苏打水替代', def: 'none' },
    { key: 'gingerbeer',name: '姜汁啤酒', covers: ['姜汁啤酒'], hint: '不能用姜汁汽水替代', def: 'none' },
    { key: 'cola',      name: '可乐',   covers: ['可乐'],   hint: '开封后冷藏，24 小时内用完', def: 'none' },
    { key: 'grapefruit',name: '西柚汁', covers: ['西柚汁'], hint: '', def: 'none' },
    { key: 'cranberry', name: '蔓越莓汁', covers: ['蔓越莓汁'], hint: '', def: 'none' },
    { key: 'grenadine', name: '石榴糖浆', covers: ['石榴糖浆'], hint: '', def: 'none' },
    { key: 'cream',     name: '淡奶油', covers: ['淡奶油'], hint: '乳脂 35%+，别用牛奶替代；开封后冷藏 3–5 天', def: 'none' },
    { key: 'mint',      name: '薄荷叶', covers: ['薄荷叶'], hint: '留兰香，自种或买', def: 'none' },
    { key: 'coffee',    name: '咖啡（现冲/冷萃）', covers: ['热咖啡', '冷萃咖啡'], hint: '', def: 'none' },
    { key: 'espresso',  name: '浓缩咖啡', covers: ['浓缩咖啡'], hint: '需咖啡机或摩卡壶', def: 'none' },
    { key: 'eggwhite',  name: '蛋白',   covers: ['蛋白'],   hint: '可选，需巴氏杀菌', def: 'none', optional: true },
    { key: 'dryvermouth', name: '干味美思-马天尼', covers: ['干味美思'], hint: '开瓶后必须冷藏，3–4 周内用完', def: 'none' },
    { key: 'sweetvermouth', name: '甜味美思-马天尼', covers: ['甜味美思'], hint: '开瓶后必须冷藏，3–4 周内用完', def: 'none' },
    { key: 'bitters',   name: '苦精',   covers: ['苦精', '安格斯特拉苦精'], hint: 'Angostura，几滴就够，一瓶能用很久', def: 'none' }
  ]
};
