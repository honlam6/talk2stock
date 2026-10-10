# jshERP REST API 参考（实测验证）

Base: `http://localhost:9999/jshERP-boot`
所有写操作带请求头 `X-Access-Token: <token>`，`Content-Type: application/json`。

## 鉴权
- `POST /user/login`  body `{"loginName":"jsh","password":"<MD5(明文)>"}`
  - 密码必须是 MD5(明文)，例如 123456 -> `e10adc3949ba59abbe56e057f20f883e`
  - 返回 `data.token`（格式如 `d6a0455bf89744b39c9969d99b43d7f0_63`，下划线后是 userId）
- 关验证码：`UPDATE jsh_platform_config SET platform_value='0' WHERE platform_key='checkcode_flag';`（立即生效）
- 登出 `/user/logout`

## 主数据（增改删，字段见 DB 表）
- 商品 `POST /material/add` · `PUT /material/update` · `GET /material/list?pageNo=1&pageSize=100`
- 商品分类 `/materialCategory/add`
- 往来单位 `POST /supplier/add`（body 含 `type:'供应商'|'客户'`）· `GET /supplier/list?type=供应商`
- 仓库 `/depot/add` · `/depot/list`
- 单位 `/unit/add`
- 账户 `/account/add` · `/account/list`
- 用户/角色/权限 `/user/*` `/role/*`（多租户 tenant_id=63 是 jsh 租户）

## 核心：建单据 POST /depotHead/addDepotHeadAndDetail
body = `{"info": "<json字符串>", "rows": "<json数组字符串>"}`
（两层都是字符串化 JSON，不是嵌套对象）

info 字段：
```
{
  "type":"入库",            // 入库/出库
  "subType":"采购",         // 采购/销售/其它/零售/调拨/盘点录入...
  "organId": <往来单位id>,
  "operTime":"2026-08-01 12:00:00",
  "number":"CGRK202608010001",   // 单号，建议用 /sequence/buildNumber?type=CGRK 生成
  "accountId": <账户id>,
  "changeAmount": 40000,    // 本次付款
  "totalPrice": 40000,
  "payType":"现付",         // 现付/预付款
  "remark":"",
  "status":"1"              // 1=审核(联动库存) 0=草稿
}
```
rows（数组，每个明细）：
```
[{
  "materialId": <商品id>,
  "barCode": "<必须存在 material_extend 的条码>",
  "name":"商品名",
  "operNumber": 50,        // 数量
  "unit":"卷",
  "unitPrice": 800,
  "allPrice": 40000,
  "depotId": <仓库id>,
  "remark":""
}]
```
其它单据类型（同一个接口，靠 type/subType 区分）：
- 销售出库：type=出库, subType=销售
- 其它入库：type=入库, subType=其它
- 其它出库：type=出库, subType=其它
- 调拨：subType=调拨（需 another_depot_id）
- 采购退货/销售退货/零售 同理

审核/反审核：`POST /depotHead/batchSetStatus` body `{"ids":"1,2","status":"1"}`
删除：`DELETE /depotHead/delete?id=...` 或 `/deleteBatch?ids=`

## 财务（收付款是独立单据，不随入库自动扣余额）
- `POST /accountHead/addAccountHeadAndDetail`  body 含 type('收款'/'付款'/'转账'), organId, accountId, totalPrice, eachAmount 等
- 收支项目 `/inOutItem`

## 查询
- 单据列表 `GET /depotHead/list?pageNo=1&pageSize=10&type=入库&subType=采购`（参数务必同前端，错参返回 code 300）
- 单据明细 `GET /depotHead/getDetailByNumber?number=...`
- 库存（按商品+仓库）`GET /depotItem/getMaterialDepotStockByParam?materialId=X&depotId=Y`
- 商品+库存 `GET /material/getListWithStock?currentPage=1&pageSize=50`（注意是 currentPage 不是 pageNo）
- 账户余额 `GET /account/listWithBalance`
- 应收应付对账 `GET /depotHead/debtList`
- 进销存统计 `GET /depotHead/getBuyAndSaleStatistics`

## 已验证的最小可用 Python 客户端
见 scripts/jsh_erp_api_client.py（含 MD5 登录、建采购入库、查库存）。

## 数据表关键外键（直接 SQL 造数时用）
- 库存：`jsh_material_current_stock(material_id, depot_id, current_number, current_unit_price, tenant_id)`
- 条码在 `jsh_material_extend(material_id, bar_code, ...)` —— 建单必查此表
- 所有业务表都有 `tenant_id`（jsh=63）；造数必须带对租户否则界面看不到
- 演示数据导入可用 `LAST_INSERT_ID()` 捕获自增 id 再组装关联表（见会话中 /tmp/jsh_demo_data.sql 思路）
