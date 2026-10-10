# jshERP API Endpoint Map (verified 2026-08-01)

Backend base: `http://localhost:9999/jshERP-boot`. Auth header `X-Access-Token`.
Login: `POST /user/login` `{"loginName":"jsh","password":<md5("123456")>}` → `data.token`.
Captcha: `jsh_platform_config.checkcode_flag='0'` disables it (DB update, no restart).

## 368 endpoints across 30 controllers. Core ones for an Agent:

### Auth
- POST /user/login  (password = MD5 of plaintext, NOT plaintext)
- GET  /user/randomImage  (captcha image; skip if checkcode_flag=0)
- GET  /user/logout

### Master data — create (NEVER send tenantId in body; backend injects it)
- POST /material/add        fields: name, model, standard, unit, categoryId, enabled
- POST /materialCategory/add  name, categoryLevel, parentId
- POST /supplier/add        supplier, type('供应商'|'客户'), enabled
- POST /depot/add           name, enabled
- POST /unit/add            basicUnit, otherUnit, ratio(REQUIRED BigDecimal), name, enabled
- POST /account/add         name, initialAmount, currentAmount, enabled
- POST /materialsExtend/add  materialId, barCode, commodityUnit, defaultFlag  ← barcode lives HERE, not on material

### Business documents
- POST /depotHead/addDepotHeadAndDetail  body {"info":JSONstr,"rows":JSONarr}
    info: type('入库'|'出库'), subType('采购'|'销售'|'其它'|'调拨'|'盘点录入'), organId, operTime("YYYY-MM-DD HH:MM:SS"), number, accountId, changeAmount, totalPrice, payType('现付'|'预付款'), status('0'draft|'1'audited)
    rows[]: materialId, barCode(MUST match a real material_extend), name, operNumber, unit, unitPrice, allPrice, depotId
- PUT  /depotHead/updateDepotHeadAndDetail
- POST /depotHead/batchSetStatus  (audit/reverse-audit: status batch update)
- DELETE /depotHead/delete, /deleteBatch
- POST /accountHead/addAccountHeadAndDetail  (收款/付款/转账 — separate from purchase-in payment)

### Queries
- GET /material/list, /material/getListWithStock
- GET /supplier/list?type=供应商|客户
- GET /depot/list, /depot/getAllListWithStock?currentPage=1&pageSize=50&depotIds=
- GET /account/list, /account/listWithBalance
- GET /depotHead/list?type=入库&subType=采购 (URL-encode Chinese)
- GET /depotItem/getMaterialDepotStockByParam?materialId=&depotId=  (param is materialId, not mId)
- GET /depotHead/debtList  (AR/AP reconciliation)
- GET /depotHead/getBuyAndSaleStatistics

## Failure modes seen this session (and their real cause)
| Symptom | Cause | Fix |
|---|---|---|
| login 500012 "验证码不能为空" | captcha on | set checkcode_flag=0 |
| add 301 "数据写入异常" / "Column 'tenant_id' specified twice" | tenantId sent in body | omit tenantId |
| unit.add NPE | ratio (BigDecimal) missing | send ratio:1 |
| addDepotHeadAndDetail 8000026 "商品条码不存在" | barCode on row not in material_extend | create materialsExtend first, use its barCode |
| addDepotHeadAndDetail "Value for barCode cannot be null" | barCode null in row | never pass null; pass the extend barcode string |
| stock didn't change after doc | status was 0 (draft), not 1 (audited) | set status:'1' |
| account balance unchanged after purchase-in | purchase-in payment ≠ account debit; separate 收付款单 | use /accountHead for money movement |

## Verified end-to-end run (the "美宜佳买3瓶可乐3.5" test)
1. login (MD5 pwd) → token
2. POST /unit/add {basicUnit:瓶,otherUnit:个,ratio:1,name:...,enabled:true} → 200
3. POST /supplier/add {supplier:美宜佳,type:供应商,enabled:1} → 200
4. POST /material/add {name:可乐,model:350ml,standard:瓶装,unit:瓶,categoryId:17,enabled:1} → 200, get id
5. POST /materialsExtend/add {materialId:<id>,barCode:BC<id>,commodityUnit:瓶,defaultFlag:1} → 200
6. POST /depotHead/addDepotHeadAndDetail {info:{type:入库,subType:采购,organId,operTime,number,accountId:25,changeAmount:10.5,totalPrice:10.5,payType:现付,status:1}, rows:[{materialId,barCode:BC<id>,name:可乐,operNumber:3,unit:瓶,unitPrice:3.5,allPrice:10.5,depotId}]} → 200
7. verify: jsh_material_current_stock shows material in depot with +3; jsh_depot_head has the doc with status=1.
