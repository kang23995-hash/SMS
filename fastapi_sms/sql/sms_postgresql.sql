-- =====================================================================
--  短信监测系统 —— PostgreSQL 初始化脚本
--  适用：PostgreSQL 12+（本脚本全部语句幂等，可重复执行）
--  执行：psql -U postgres -f sql/sms_postgresql.sql
--       或在 Navicat / DBeaver / pgAdmin 里「作为脚本整体执行」
--
--  配套的服务端配置（.env）：
--      DB_TYPE=postgresql
--      DB_HOST=127.0.0.1
--      DB_PORT=5432
--      DB_USER=postgres
--      DB_PASSWORD=你的密码
--      DB_NAME=sms_monitor
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. 创建数据库（可选；你也可以用图形工具手动建库）
--
--    注意：PostgreSQL 的 CREATE DATABASE 不能放在事务块里执行，
--    请把本句单独执行一次。TEMPLATE template0 + ENCODING 'UTF8'
--    保证不论 template1 的默认编码是什么，都能建成 UTF8 库
--    （utf8 才能完整存储 emoji 与生僻字，短信里很常见）。
-- ---------------------------------------------------------------------
CREATE DATABASE sms_monitor
    WITH ENCODING 'UTF8'
    TEMPLATE template0;

-- 切换到刚建的库。
--   * 命令行 psql：下面这行会自动切换。
--   * Navicat / DBeaver / pgAdmin：请手动选中 sms_monitor 库后再执行第 2 节起的语句。
\c sms_monitor


-- ---------------------------------------------------------------------
-- 2. 创建短信记录表
--
--    与 MySQL 版的对应关系：
--      BIGSERIAL      ≈ BIGINT UNSIGNED AUTO_INCREMENT（自增主键）
--      SMALLINT       ≈ TINYINT
--      TIMESTAMP      ≈ DATETIME（不带时区）
--      COMMENT ON     ≈ 列/表注释
--
--    索引设计说明（直接影响并发写入与分页查询性能）：
--      * PRIMARY KEY (id)                     —— 自增主键
--      * uk_message_unique_id（唯一索引）      —— 消息去重的核心。入库用
--            INSERT ... ON CONFLICT (message_unique_id) DO NOTHING，
--            靠它实现「重复消息不重复入库」，去重判断为索引级 O(log n)
--      * idx_receive_time                     —— 列表页默认按接收时间倒序，必需
--      * idx_device_receive(device_id, receive_time)
--                                             —— 覆盖「按设备筛选 + 时间倒序」的高频组合
--      * idx_sender / idx_sim_phone           —— 支撑发送号码、手机号筛选
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sms_records
(
    id                BIGSERIAL    PRIMARY KEY,
    message_unique_id VARCHAR(64)  NOT NULL,
    device_id         VARCHAR(128) NOT NULL,
    sim_slot          SMALLINT     NOT NULL DEFAULT 0,
    sim_phone_number  VARCHAR(32)  NOT NULL DEFAULT '',
    sender            VARCHAR(64)  NOT NULL DEFAULT '',
    sender_name       VARCHAR(64)  NOT NULL DEFAULT '',
    message_content   TEXT         NOT NULL,
    sms_vcode         VARCHAR(16)  NOT NULL DEFAULT '',
    receive_time      TIMESTAMP    NOT NULL,
    create_time       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 唯一索引：消息去重的核心（ON CONFLICT 依赖它）
CREATE UNIQUE INDEX IF NOT EXISTS uk_message_unique_id
    ON sms_records (message_unique_id);

-- 普通索引
CREATE INDEX IF NOT EXISTS idx_receive_time   ON sms_records (receive_time);
CREATE INDEX IF NOT EXISTS idx_device_id      ON sms_records (device_id);
CREATE INDEX IF NOT EXISTS idx_device_receive ON sms_records (device_id, receive_time);
CREATE INDEX IF NOT EXISTS idx_sender         ON sms_records (sender);
CREATE INDEX IF NOT EXISTS idx_sim_phone      ON sms_records (sim_phone_number);
CREATE INDEX IF NOT EXISTS idx_sms_vcode      ON sms_records (sms_vcode);


-- ---------------------------------------------------------------------
-- 3. 表 / 列注释（PostgreSQL 用 COMMENT ON 代替 MySQL 的行内 COMMENT）
-- ---------------------------------------------------------------------
COMMENT ON TABLE  sms_records                    IS '短信记录表';
COMMENT ON COLUMN sms_records.id                 IS '自增主键';
COMMENT ON COLUMN sms_records.message_unique_id  IS 'APP 生成的唯一消息 ID（UUID），用于去重';
COMMENT ON COLUMN sms_records.device_id          IS '设备唯一标识';
COMMENT ON COLUMN sms_records.sim_slot           IS 'SIM 卡槽编号：1/2，0 表示未知';
COMMENT ON COLUMN sms_records.sim_phone_number   IS '本机 SIM 号码（已去掉 +86 区号），读不到时为空串';
COMMENT ON COLUMN sms_records.sender             IS '发送方号码';
COMMENT ON COLUMN sms_records.sender_name        IS '发送方名称：短信签名【xxx】里的 xxx，提取不到为空串';
COMMENT ON COLUMN sms_records.message_content    IS '短信正文';
COMMENT ON COLUMN sms_records.sms_vcode          IS '从正文提取的验证码（长度自适应，提取不到为空串）';
COMMENT ON COLUMN sms_records.receive_time       IS '短信接收时间（已按 APP_TIMEZONE 归一化）';
COMMENT ON COLUMN sms_records.create_time        IS '入库时间（数据库默认当前时间）';


-- =====================================================================
-- 4. 给「已存在的旧表」补列 / 补索引
--
--    全部带 IF NOT EXISTS，重复执行也不会报错，可以放心整段执行，
--    用来把早期建的旧表升级到最新结构。
-- =====================================================================
ALTER TABLE sms_records
    ADD COLUMN IF NOT EXISTS sender_name VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE sms_records
    ADD COLUMN IF NOT EXISTS sms_vcode VARCHAR(16) NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS uk_message_unique_id
    ON sms_records (message_unique_id);
CREATE INDEX IF NOT EXISTS idx_sms_vcode ON sms_records (sms_vcode);


-- =====================================================================
-- 5. 可选：为服务专门建一个受限账号（推荐，不要用 postgres 超级用户跑业务）
--    把 'YourStrongPassword' 换成你自己的强密码，
--    并同步修改 .env 里的 DB_USER / DB_PASSWORD。
-- =====================================================================
-- CREATE USER sms_app WITH PASSWORD 'YourStrongPassword';
-- GRANT CONNECT ON DATABASE sms_monitor TO sms_app;
-- GRANT USAGE ON SCHEMA public TO sms_app;
-- -- 建表后授权（若之后又新建表，需对新表再授一次权）：
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sms_app;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sms_app;
-- -- 让以后新建的表/序列默认也带上权限：
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public
--     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sms_app;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public
--     GRANT USAGE, SELECT ON SEQUENCES TO sms_app;


-- =====================================================================
-- 6. 建表结果自检（执行完上面的语句后运行，确认表与索引都在）
-- =====================================================================
-- \d sms_records
-- \di sms_records*
-- -- 查看最近几条的解析结果，验证提取是否正常：
-- SELECT id, sender_name, sms_vcode, message_content
-- FROM sms_records ORDER BY id DESC LIMIT 5;
