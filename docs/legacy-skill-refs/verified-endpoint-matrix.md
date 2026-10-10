# jshERP API 已验证端点矩阵（实测，纯 API 无 SQL 兜底）

> 来源：2026-08 一次完整验证会话。所有场景均通过 curl 真实调用确认 code 200。
> 目的：给运行 jshERP 的 Agent（尤其低成本模型）一份"照表填参"的权威清单。

## 一、单据 type/subType 精确映射（建单必填，错一个就报错）

| 业务 | type | subType | 备注 |
|------|------|---------|------|
| 采购入库 | 入库 | 采购 | 货进，库存+ |
| 销售出库 | 出库 | 销售 | 货出，库存- |
| 采购退货 | 出库 | 采购退货 | 退给供应商，库存- |
| 销售退货 | 入库 | 销售退货 | 客户退回，库存+ |
| 其他入库 | 入库 | 其它 | |
| 其他出库 | 出库 | 其它 | |
| 调拨 | 出库 | 调拨 | 需 anotherDepotId（目标仓）|
| 盘点录入 | 入库 | 盘点录入 | |
| 采购订单 | 其它 | 采购订单 | 不含金额，可转采购入库 |
| 销售订单 | 其它 | 销售订单 | 不含金额，可转销售出库 |

## 二、建单接口统一格式

```
POST /depotHead/addDepotHeadAndDetail
Content-Type: application/json
X-Access-Token: <token>
{
  "info": "<JSON字符串，不是对象>",
  "rows": "<JSON数组字符串，不是数组>"
}
```
- info 必填：type, subType, organId, operTime, number, accountId, changeAmount, totalPrice, payType, status(1审核/0草稿), remark
- rows 每行必填：materialId, barCode(来自 materialsExtend), name, operNumber, unit, unitPrice, allPrice, depotId
- 调拨单 rows 额外加 anotherDepotId

## 三、全场景验证结果（15 场景全部 通过）

| # | 场景 | 验证点 | 结果 |
|---|------|--------|------|
| 1 | 销售出库 | 客户新增+商品新增+不指定仓库(默认成品仓) | 库存联动 |
| 2 | 采购入库 | 供应商新增+商品新增+不指定仓库(默认原料仓) | 通过 |
| 3 | 收款单 | billNo 放主表 info | 通过 |
| 4 | 其他入库 | 最简单单据 | 通过 |
| 5 | 其他出库 | - | 通过 |
| 6 | 采购退货 | type=出库 | 通过 |
| 7 | 销售退货 | type=入库 | 通过 |
| 8 | 调拨 | 跨仓库 anotherDepotId | 通过 |
| 9 | 盘点录入 | - | 通过 |
| 10 | 采购订单 | type=其它 | 通过 |
| 11 | 销售订单 | type=其它 | 通过 |
| 12 | 多账户付款 | accountIdList + accountMoneyList | 通过 |
| 13 | 审核/反审核 | POST /depotHead/batchSetStatus {ids,status} | 通过 |
| 14 | 账户余额联动 | 收款单后查 current_amount | 不变（原设计，非 bug）|
| 15 | 列表查询 | search={type,subType} 包裹参数 | 通过（平铺参数会 300）|

## 四、"不稳定接口"根因（均非 jshERP bug，是调用方式错）

| 现象 | 误判 | 真因 / 正确调用 |
|------|------|----------------|
| add 接口返回 301 / tenant_id specified twice | 接口有 bug | body 多传了 tenantId；删掉即可（系统从 token 注入）|
| 建单 500 / barCode cannot be null | 商品建好了 | 建商品后未建 materialsExtend 条码；需补 POST /materialsExtend/add |
| 收付款 500 / billNo cannot be null | 字段名错 | billNo 要放 info 主表，不是 rows |
| 列表 300 / 数据查询异常 | jshERP 不稳 | /depotHead/list 的查询条件要包进 search={...} JSON，不能平铺 type/subType |
| 登录失败 / user password error | 密码错 | 密码需先 MD5 再传 |

## 五、建主数据接口清单（均不传 tenantId）

- 单位：POST /unit/add {basicUnit,otherUnit,ratio,name,enabled}（ratio 必填，否则 NPE）
- 往来单位：POST /supplier/add {supplier,type:供应商|客户,enabled}
- 仓库：POST /depot/add {name,enabled}
- 商品：POST /material/add {name,model,standard,unit,categoryId,enabled}
- 条码：POST /materialsExtend/add {materialId,barCode:BC<id>,commodityUnit,defaultFlag:1}
- 账户：POST /account/add {name,initialAmount,currentAmount,enabled}

## 六、给低成本模型的兜底

优先用封装脚本（同 skill 的 scripts/jsh_api.py）而非手拼 JSON。该脚本已处理：MD5 登录、去 tenantId、自动建条码、search 参数查询、默认仓选择。

## 七、用户明确的设计原则（不要违反）

- "你直接改 SQL，网页上面又可能没有记录了" → 永远走 API，不裸写 SQL
- "如果是原本设计好的，那就按原本设计好的，因为我是门外汉" → 不擅自改 jshERP 源码（不修负库存拦截、不修余额联动），用查询+提醒代替系统级拦截
