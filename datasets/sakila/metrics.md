# sakila 指标口径（候选 · 待人工确认）

> 由 `scripts/onboard.py` 于 2026-10-08 20:37 自动生成。
> **口径是业务定义，自动生成的内容只是候选**：下面「表关系」一节读自库、属事实；
> 其余标注【待确认】的口径假设必须由业务方核对后，才能当作正式口径使用。
> 在人工确认前，这些口径**不会**被口径守卫当作强制规则（见 chatbi/datasets.py）。

## 表关系（外键约束，读自库、属事实）

- address.city_id -> city.city_id
- city.country_id -> country.country_id
- customer.address_id -> address.address_id
- customer.store_id -> store.store_id
- film.language_id -> language.language_id
- film.original_language_id -> language.language_id
- film_actor.actor_id -> actor.actor_id
- film_actor.film_id -> film.film_id
- film_category.category_id -> category.category_id
- film_category.film_id -> film.film_id
- inventory.film_id -> film.film_id
- inventory.store_id -> store.store_id
- payment.customer_id -> customer.customer_id
- payment.rental_id -> rental.rental_id
- payment.staff_id -> staff.staff_id
- rental.customer_id -> customer.customer_id
- rental.inventory_id -> inventory.inventory_id
- rental.staff_id -> staff.staff_id
- staff.address_id -> address.address_id
- staff.store_id -> store.store_id
- store.address_id -> address.address_id
- store.manager_staff_id -> staff.staff_id

## 口径候选

# Sakila数据库指标口径候选清单

## 一、核心业务实体与粒度

### 【事实】
1. **actor（演员）**: 一人一行，主键 `actor_id`。通过 `film_actor` 与 `film` 关联。
2. **customer（顾客）**: 一人一行，主键 `customer_id`。有 `active` 字段标识有效性。
3. **film（电影）**: 一部电影一行，主键 `film_id`。有多维属性：语言、评级、成本、时长、租金等。
4. **rental（租赁订单）**: 一次租赁行为一行，主键 `rental_id`。核心事实表，包含租赁开始日期 (`rental_date`) 和归还日期 (`return_date`）。
5. **payment（支付记录）**: 一次支付一行，主键 `payment_id`。通常与 `rental` 通过外键 `rental_id` 关联。
6. **inventory（库存）**: 一份独立的物理拷贝（如一个DVD）一行，主键 `inventory_id`。同一部电影（`film_id`）可在同一或不同商店有多个库存。
7. **store（商店）**: 一家店一行，主键 `store_id`。
8. **staff（员工）**: 一人一行，主键 `staff_id`。担任 `store` 的管理员，也处理 `rental` 和 `payment`。
9. **category（电影类别）**: 一个类别一行，主键 `category_id`。通过 `film_category` 与 `film` 关联。

### 【待确认】
1. **顾客活跃状态**：指标计算时，是否仅包括 `active = 1` 的顾客？
2. **租赁订单状态**：`return_date` 为 `NULL` 是否代表“租出未归还”？用于统计时是否应视为有效订单？

## 二、时间基准候选

### 【事实】
1. **创建/发生时间**：
   - 顾客注册时间: `customer.create_date`
   - 租赁开始时间: `rental.rental_date`
   - 支付时间: `payment.payment_date`
   - 租赁归还时间: `rental.return_date`
2. **最后更新时间**：几乎所有表都有 `last_update` 字段，记录行级变更时间。

### 【待确认】
1. **核心业务日期**：分析租赁业务时，默认的时间基准是 `rental_date`（租赁发生）还是 `payment_date`（支付发生）？例如，计算日租金收入。
2. **归还日期处理**：分析租赁周期时，如果 `return_date` 为 `NULL`，应赋予何种默认值或将其排除统计？

## 三、金额/数量口径候选

### 【事实】
1. **支付金额**：`payment.amount` (decimal(5,2))，为单笔支付金额。
2. **电影租金标准**：`film.rental_rate` (decimal(4,2))，为单部电影单次租赁标准价格。
3. **电影置换成本**：`film.replacement_cost` (decimal(5,2))。
4. **租赁时长**：`film.rental_duration` （天），为单部电影的标准租赁期限。
5. **电影时长**：`film.length` （分钟）。

### 【待确认】
1. **金额单位**：所有金额字段的单位是否已统一（如美元）？
2. **金额是否含税**：`payment.amount` 是否已为顾客实际支付的含税金额？
3. **统计逻辑**：
   - 计算“总收入”时，是否简单对 `payment.amount` 求和？是否存在部分支付记录无效（如后续退款）的情况？（表中无退款标识）
   - 计算“单均租金收入”时，分子用 `payment.amount` 总和，分母是按 `rental_id` 还是 `payment_id` 计数？需厘清 `payment` 与 `rental` 是否为 1:1 关系。

## 四、去重陷阱 (1:N 关联)

### 【事实】
以下为常见的一对多关系，在 `COUNT`、`SUM` 等聚合时若不注意关联粒度，极易导致结果放大：

1. **`film` ↔ `film_actor` → `actor`**: 一部电影有多个演员。统计演员数量或关联演员信息时，直接 `JOIN` 会使 `film` 行数膨胀。
2. **`film` ↔ `film_category` → `category`**: 一部电影可属多个类别？(根据复合主键设计，是1:N)。
3. **`film` ↔ `inventory`**: 一部电影有多份库存拷贝。统计电影租赁次数时，若通过 `inventory` 关联 `rental`，需注意是否按 `film` 聚合去重。
4. **`customer` ↔ `rental`**: 一位顾客有多次租赁。统计顾客消费时，简单 `JOIN` 会放大顾客数量。
5. **`rental` ↔ `payment`**: 一笔租赁是否对应多笔支付？(表中外键 `payment.rental_id` 可空且为索引，可能为 1:0/1/N 关系)。**此为关键待确认点**。

### 【待确认】
1. **支付与租赁的关系**：`payment` 与 `rental` 是严格的一对一关系吗？是否存在一笔 `rental` 对应多笔 `payment`（如分期支付）或一笔 `payment` 涵盖多笔 `rental`（如合并支付）的场景？
2. **唯一计数键**：
   - 统计“租赁的电影部数”时，去重键是 `film_id` 还是 `inventory_id`？前者统计“被租赁过的不同影片”，后者统计“发生的物理租赁次数”。
   - 统计“活跃顾客数”时，去重键是 `customer_id`。

## 五、常用分析维度与指示字段

### 【事实】
1. **电影维度**：
   - 分级：`film.rating` (G, PG, PG-13, R, NC-17)。
   - 语言：`film.language_id` → `language.name`。
   - 特殊特征：`film.special_features` (set 类型)。
   - 发行年份：`film.release_year`。
2. **顾客维度**：
   - 所属商店：`customer.store_id`。
   - 活跃状态：`customer.active` (0/1)。
3. **空间维度**：
   - 顾客地址 → 所在区域：`customer.address_id` → `address` → `city` → `country`。
   - 商店地址：`store.address_id` → 同上链路。
4. **时间维度**：
   - 年/月/日/星期：可从 `rental.rental_date`, `payment.payment_date` 等日期字段提取。
5. **员工/商店维度**：
   - 处理员工：`rental.staff_id`, `payment.staff_id`。
   - 所属/管理商店：`staff.store_id`, `store.manager_staff_id`。

### 【待确认】
1. **顾客归属地**：顾客分析时，常用地理维度是基于其注册地址 (`customer.address_id`) 还是基于其所属商店的地址？
2. **电影类别分析**：由于电影可属多个类别，计算“各类别电影数量”时，分子是电影数量（需去重）还是电影-类别关联记录数？
