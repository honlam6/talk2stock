# jshERP API 端点清单（实测版）

源码扫描：`jshERP-boot/src/main/java/com/jsh/erp/controller/` 共 **30 个 Controller / 339 个端点**。
本文档只收录**实际调通过**的接口，附真实返回样例。未实测的按模块列出路径供查阅。

通用规则见 `字段必填清单.md` 与 skill `jsherp`。所有请求带 `X-Access-Token` 头。

---

## 一、排错第一原则

500 报错**几乎都是缺必填参数**，不是接口坏。查真实原因：

```bash
docker logs erp-backend --since 2m 2>&1 | grep "msg :" | sort -u
```

会直接打印 `Required String parameter 'xxx' is not present`。比读源码快得多。

⚠️ 分页参数不统一：有的接口用 `pageNo`，统计类接口用 **`currentPage`**。

---

## 二、实测可用的统计/报表接口（老板看板可直接用）

### 1. 采购统计　`GET /depotItem/buyIn`
参数：`beginTime` `endTime`（`YYYY-MM-DD HH:MM:SS`）`currentPage` `pageSize` `materialParam`(可空) `organId`(可空)
```json
{"code":200,"data":{"total":10,"realityPriceTotal":85690.0,"rows":[...]}}
```
→ `realityPriceTotal` 就是区间内采购总额。**看板核心接口。**

### 2. 销售统计　`GET /depotItem/saleOut`
参数同上。
```json
{"code":200,"data":{"total":3,"realityPriceTotal":2800.0,"rows":[...]}}
```

### 3. 零售统计　`GET /depotItem/retailOut`（参数同上）

### 4. 进销存统计　`GET /depotItem/getInOutStock`
参数：`currentPage` `pageSize` `beginTime` `endTime` `materialParam` `depotId`
```json
{"code":200,"data":{"total":13,"rows":[{"unitName":"箱","thisSum":7.0,"unitPrice":23.5,...}]}}
```
→ 每个商品的期初/入库/出库/结存。

### 5. 进销存总金额　`GET /depotItem/getInOutStockCountMoney`
参数：`beginTime` `endTime` `materialParam` `depotId`
```json
{"code":200,"data":{"showStockPrice":true,"totalStock":75.0,"totalCount":5490.0}}
```
→ 库存总量与总货值。

### 6. 月度趋势　`GET /depotItem/buyOrSalePrice`　**无需参数**
```json
{"code":200,"data":{"buyPriceList":[{"x":"2026-03","y":0},...],
                    "salePriceList":[...],"retailPriceList":[...]}}
```
→ 近 6 个月采购/销售/零售金额，直接画折线图。

### 7. 欠款单据　`GET /depotHead/debtList`
参数：`currentPage` `pageSize`
```json
{"code":200,"data":{"total":2,"rows":[{"id":292,"debt":-100.0,"needDebt":-1,...}]}}
```
→ 应收应付明细。注意 `debt` 可能为负。

### 8. 待收付款条数　`GET /depotHead/getNeedCount`
参数：**`type` 必填**（`收款` / `付款`）
```json
{"code":200,"data":{"needCount":0}}
```

### 9. 账户余额报表　`GET /account/listWithBalance`
参数：`name`(可空) `serialNo`(可空) `currentPage` `pageSize`
```json
{"code":200,"data":{"total":5,"rows":[{"name":"测试账户Y","currentAmount":40.0,"thisMonthAmount":"40.0",...}]}}
```

### 10. 账户总计　`GET /account/getStatistics`
参数：`name`(可空) `serialNo`(可空)
```json
{"code":200,"data":{"allCurrentAmount":"340578.0","allMonthAmount":"88890.0"}}
```

### 11. 仓库库存分布　`GET /depot/getAllListWithStock`
参数：**`mId` 必填**（商品 id）
```json
{"code":200,"data":[{"id":23,"name":"原料仓B","initStock":0,"currentStock":9.0}]}
```

### 12. 待入出库单据数　`GET /depotHead/waitBillCount`　无需参数
```json
{"code":200,"data":{"total":23,"message":"成功"}}
```

---

## 三、实测可用的基础/业务接口

| 端点 | 方法 | 用途 | 关键参数 |
|------|------|------|---------|
| `/user/login` | POST | 登录 | `loginName` + **MD5** 密码 |
| `/supplier/list` | GET | 供应商/客户列表 | `search={"type":"供应商"}` |
| `/supplier/add` | POST | 新建往来单位 | `supplier` `type` `enabled` |
| `/material/list` | GET | 商品列表（**行自带 `stock` 实时库存**） | `search={}` |
| `/material/add` | POST | 新建商品 | `name` `unit` `categoryId`(可 null) |
| `/materialsExtend/add` | POST | 建条码（**建商品后必须**） | `materialId` `barCode` `defaultFlag` |
| `/materialsExtend/getDetailList` | GET | 查条码 | **平铺** `?materialId=x`（用 search 会 500） |
| `/depot/list` | GET | 仓库列表 | — |
| `/depot/add` | POST | 新建仓库 | `name` `enabled` |
| `/account/list` | GET | 结算账户 | — |
| `/unit/list` `/unit/add` | GET/POST | 计量单位 | — |
| `/inOutItem/list` | GET | 收支项目 | — |
| `/depotHead/list` | GET | 单据列表 | **`subType` 必传**，否则后端空指针 |
| `/depotHead/addDepotHeadAndDetail` | POST | 建单（核心） | `info` + `rows`（字符串化 JSON） |
| `/accountHead/addAccountHeadAndDetail` | POST | 收付款单 | `billNo` 放 info 主表 |
| `/sequence/buildNumber` | GET | 生成单号 | `type=CGRK` 等前缀 |
| `/materialCategory/getAllList` | GET | 商品分类 | **`?parentId=0`** 必填 |

---

## 四、实测失败（已知需更多参数或环境依赖）

| 端点 | 现象 | 备注 |
|------|------|------|
| `/depotHead/getBuyAndSaleStatistics` | 500 获取数据失败 | 官方首页统计，疑似依赖特定初始化数据；已改用 `buyIn`+`saleOut` 替代 |
| `/depotItem/findStockWarningCount` | 500 | 补了 currentPage/materialParam/depotId 仍失败；已改用 `material/list` 的 `stock` 字段自建预警 |
| `/depotHead/getStatementAccount` | 500 | 对账单，参数组合待试 |
| `/depotHead/findInOutMaterialCount` | 500 | 入出库汇总 |
| `/depotHead/findInOutDetail` | 500 | 入出库明细 |
| `/material/getListWithStock` | 500 | 已有替代：`/material/list` 自带 stock |

> 这些不影响看板，因为都有可用替代路径。

---

## 五、按模块的完整端点分布（339 个）

| 模块 | 前缀 | 端点数 |
|------|------|--------|
| 单据管理 | `/depotHead` | 27 |
| 商品管理 | `/material` | 27 |
| 商家管理（供应商/客户） | `/supplier` | 23 |
| 用户管理 | `/user` | 22 |
| 账户管理 | `/account` | 15 |
| 仓库管理 | `/depot` | 14 |
| 单据明细 | `/depotItem` | 14 |
| 系统参数 | `/systemConfig` | 14 |
| 平台参数 | `/platformConfig` | 13 |
| 经手人 | `/person` | 13 |
| 功能管理 | `/function` | 12 |
| 角色管理 | `/role` | 12 |
| 商品类别 | `/materialCategory` | 11 |
| 用户角色关系 | `/userBusiness` | 11 |
| 单位 / 商品属性 / 扩展字段 / 收支项目 / 财务管理 / 部门 | 各 10 | 60 |
| 商品价格扩展 | `/materialsExtend` | 9 |
| 租户 | `/tenant` | 9 |
| 消息 | `/msg` | 8 |
| 日志 | `/log` | 7 |
| 字典 / 序列号 / 单据编号 / 财务明细 / 插件 | — | 16 |

每个 CRUD 模块的标准端点：`/info` `/list` `/add` `/update` `/delete` `/deleteBatch`
`/checkIsNameExist` `/findBySelect` `/batchSetStatus`。
