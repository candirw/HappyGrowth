# Cocktail Journal · 鸡尾酒品鉴记录

一个**零依赖、纯前端、双击即用**的家庭调酒记录工具。给喜欢在家调酒、又想把口感量化下来的人用。

不联网、不注册、不上传任何数据——所有记录都存在你自己浏览器的 localStorage 里。

---

## 它能做什么

**① 八维打分，基酒和鸡尾酒分开算**

5 分制（可打 0.5 档），6 个通用维度 + 各 2 个专属维度：

| 通用 | 基酒专属 | 鸡尾酒专属 |
|---|---|---|
| 香气 / 口感 / 风味 / 平衡 / 余韵 / 个人偏好 | 纯净度 / 品类辨识度 | 酒感融合 / 复现意愿 |

**② 34 款配方的详细指南**

每款含杯型、冰、技法、耗时、酒精度估算、分步操作、装饰、关键点、常见错误。其中 16 款对照 IBA（国际调酒师协会）官网逐条核校过。

**③ 按你的库存自动判断「能不能做」**

录入自己的酒和辅料后，每张配方卡会自动标 `可做` / `差 N 样` / `缺基酒`，还能一键筛选「只看能做的」。

**④ 统计画像**

六维雷达图、偏好 Top5、风味标签频次、自动生成的口味结论。

**⑤ 数据可带走**

导出 Markdown（可直接贴进笔记）、导出 / 导入 JSON 备份。

---

## 快速开始

### 方式一：直接打开（最简单）

双击 `index.html`。用 Chrome 或 Edge 打开，Firefox 也可以。

### 方式二：本地服务器（可选）

macOS / Linux：

```bash
./start.sh
```

Windows：双击 `start.bat`（只是帮你打开 `index.html`，不需要装 Python）。

> 两种方式的数据是**分开的**：`file://` 和 `http://localhost` 属于不同来源，localStorage 不互通。选定一种就别换。

---

## 改成你自己的库存

默认加载的是 `inventory.example.js`（11 瓶酒的示例配置，名称统一写成「品类-品牌」）。

```bash
cp inventory.example.js inventory.js
```

然后只改 `inventory.js`。`inventory.js` 已在 `.gitignore` 里，不会被提交上去。

最关键的字段是 `covers`——它告诉程序"这瓶酒能满足配方里的哪个用料名"：

```js
{
  code: 'S04', name: '威士忌-尊美醇',    // 命名约定：品类-品牌
  covers: ['威士忌', '爱尔兰威士忌'],   // ← 配方里写「威士忌」时，这瓶算数
  type: '爱尔兰威士忌', abv: '40%', origin: '爱尔兰',
  taste: '三次蒸馏，顺滑，带香草和淡淡果香',
  keep: '常温避光直立，开瓶后 1–2 年风味基本稳定', warn: false,
  use: '威士忌酸、威士忌苏打、教父'
}
```

`covers` 写漏了，对应配方就会显示「缺基酒」。字段全表见 [docs/customize-inventory.md](docs/customize-inventory.md)。

---

## 加自己的配方

配方都在 `recipes.js`，是一个数组，照格式加一条即可。字段说明见 [docs/recipe-format.md](docs/recipe-format.md)。

配方和库存是**解耦**的：配方只写通用用料名（`伏特加`、`青柠汁`），不绑定具体品牌。这样换酒不用改配方。

---

## 目录结构

```
cocktail-journal/
├── index.html              # 应用本体（样式与逻辑都在这一个文件里）
├── recipes.js              # 配方库，34 款
├── inventory.example.js    # 示例库存，11 瓶
├── inventory.js            # 你的库存（gitignore，需自己创建）
├── start.sh / start.bat    # 可选启动脚本
├── README.md
├── LICENSE
├── .gitignore
└── docs/
    ├── customize-inventory.md
    └── recipe-format.md
```

为什么不用打包工具？因为这类工具最重要的是**十年后还能打开**。没有 npm install，没有构建步骤，没有依赖会过期。

---

## 数据安全

- 数据只存在**当前浏览器**的 localStorage 里
- 清理浏览器数据、换浏览器、重装系统都会**丢失**
- 页面「数据」页有备份提醒：从未导出或超过 7 天会标红

**建议每记满 10 条导出一次 JSON。**

---

## 已知限制

| 限制 | 说明 |
|---|---|
| 数据不跨设备 | localStorage 是浏览器本地存储，想同步得自己导 JSON |
| 不支持多用户 | 单人工具，没有账号体系 |
| 移动端可用但未优化 | 布局会自适应，但没做 App 级交互 |
| 配方量以 ml 为准 | 不提供 oz 换算（1 oz ≈ 30 ml） |

---

## 配方来源

16 款标注 `IBA` 的配方，均对照 [iba-world.com](https://iba-world.com) 官方页面逐条核校；其余标注 `通行` 的为酒吧通用比例。

配方用量是**基准点**，不是标准答案。先按原量做一遍，再按自己口味调，一次只动一个变量。

---

## License

[MIT](LICENSE) © 2026 TerryLyu

配方本身属于公共知识，代码部分随意使用。
