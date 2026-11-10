-- =====================================================================
--  短信监测系统 —— 数据库初始化脚本
--  适用：MySQL 5.7+ / 8.0+
--  执行：mysql -u root -p < sms.sql
--       或在 Navicat / MySQL Workbench 中整体执行本文件
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. 创建数据库
--    utf8mb4 才能完整存储 emoji 与生僻字（短信里很常见）
-- ---------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS `sms_monitor`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE `sms_monitor`;

-- ---------------------------------------------------------------------
-- 2. 创建短信记录表
--
--    索引设计说明（直接影响并发写入与分页查询性能）：
--      * PRIMARY KEY (id)                     —— 自增主键，InnoDB 聚簇索引
--      * UNIQUE KEY uk_message_unique_id      —— 唯一索引，消息去重的核心。
--                                                入库用 INSERT ... ON DUPLICATE KEY，
--                                                靠它实现「重复消息不重复入库」，
--                                                同时把去重判断成本降到索引级别的 O(log n)
--      * KEY idx_receive_time                 —— 列表页默认按 receive_time 倒序，必需
--      * KEY idx_device_receive(device_id, receive_time)
--                                             —— 覆盖「按设备筛选 + 时间倒序」这个最高频组合，
--                                                避免 filesort
--      * KEY idx_sender / idx_sim_phone       —— 支撑页面上的发送号码、手机号筛选
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `sms_records`
(
    `id`                BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT           COMMENT '自增主键',
    `message_unique_id` VARCHAR(64)      NOT NULL                          COMMENT 'APP 生成的唯一消息 ID（UUID），用于去重',
    `device_id`         VARCHAR(128)     NOT NULL                          COMMENT '设备唯一标识',
    `sim_slot`          TINYINT UNSIGNED NOT NULL DEFAULT 0                COMMENT 'SIM 卡槽编号：1/2，0 表示未知',
    `sim_phone_number`  VARCHAR(32)      NOT NULL DEFAULT ''               COMMENT '本机 SIM 号码（已去掉 +86 区号），读不到时为空串',
    `sender`            VARCHAR(64)      NOT NULL DEFAULT ''               COMMENT '发送方号码',
    `sender_name`       VARCHAR(64)      NOT NULL DEFAULT ''               COMMENT '发送方名称：短信签名【xxx】里的 xxx，提取不到为空串',
    `message_content`   TEXT             NOT NULL                          COMMENT '短信正文',
    `sms_vcode`         VARCHAR(16)      NOT NULL DEFAULT ''               COMMENT '从正文提取的验证码（长度自适应，提取不到为空串）',
    `receive_time`      DATETIME         NOT NULL                          COMMENT '短信接收时间（已按 APP_TIMEZONE 归一化）',
    `create_time`       DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间（数据库默认当前时间）',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_message_unique_id` (`message_unique_id`),
    KEY `idx_receive_time` (`receive_time`),
    KEY `idx_device_id` (`device_id`),
    KEY `idx_device_receive` (`device_id`, `receive_time`),
    KEY `idx_sender` (`sender`),
    KEY `idx_sim_phone` (`sim_phone_number`),
    KEY `idx_sms_vcode` (`sms_vcode`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = '短信记录表';


-- =====================================================================
-- 3. 索引优化语句（供「表已存在」的场景单独执行）
--    如果上面是全新建表，本节无需执行；重复执行会报 "Duplicate key name"，
--    可以直接忽略，或先查 SHOW INDEX FROM sms_records; 确认是否已存在。
-- =====================================================================
-- ALTER TABLE `sms_records` ADD UNIQUE KEY `uk_message_unique_id` (`message_unique_id`);
-- ALTER TABLE `sms_records` ADD KEY `idx_receive_time` (`receive_time`);
-- ALTER TABLE `sms_records` ADD KEY `idx_device_id` (`device_id`);
-- ALTER TABLE `sms_records` ADD KEY `idx_device_receive` (`device_id`, `receive_time`);
-- ALTER TABLE `sms_records` ADD KEY `idx_sender` (`sender`);
-- ALTER TABLE `sms_records` ADD KEY `idx_sim_phone` (`sim_phone_number`);
-- -- 注意：sms_vcode 只建在本文件的新建表语句里；旧表要加该索引，
-- --       请先执行 3.1 节补列，再加索引（见 3.1 节末尾）。


-- =====================================================================
-- 3.1 给「已存在的旧表」补加新列（重要！）
--
--     如果你的 sms_records 是之前建好的、还没有 sender_name / sms_vcode
--     这两列，请把下面两条 ALTER 取消注释后执行（新装库跳过即可，
--     上面第 2 节的建表语句已经包含这两列，重复执行会报
--     "Duplicate column name"，可忽略）。
--     两列都给了 NOT NULL DEFAULT ''，加列不会影响已有数据，
--     旧行的这两个字段会先为空串，之后新入库的短信会自动填入。
-- =====================================================================
-- ALTER TABLE `sms_records`
--     ADD COLUMN `sender_name` VARCHAR(64) NOT NULL DEFAULT ''
--         COMMENT '发送方名称：短信签名【xxx】里的 xxx，提取不到为空串'
--         AFTER `sender`;
-- ALTER TABLE `sms_records`
--     ADD COLUMN `sms_vcode` VARCHAR(16) NOT NULL DEFAULT ''
--         COMMENT '从正文提取的验证码（长度自适应，提取不到为空串）'
--         AFTER `message_content`;

-- 若还要给验证码列加索引（按验证码搜索时用得上），可再执行：
-- ALTER TABLE `sms_records` ADD KEY `idx_sms_vcode` (`sms_vcode`);


-- =====================================================================
-- 4. 可选：为服务专门建一个受限账号（推荐，不要用 root 跑业务）
--    把 'YourStrongPassword' 换成你自己的强密码，
--    并同步修改 .env 里的 DB_USER / DB_PASSWORD
-- =====================================================================
-- CREATE USER 'sms_app'@'%' IDENTIFIED BY 'YourStrongPassword';
-- GRANT SELECT, INSERT, UPDATE, DELETE ON `sms_monitor`.* TO 'sms_app'@'%';
-- FLUSH PRIVILEGES;

-- 注意：MySQL 8 默认认证插件是 caching_sha2_password，
-- Python 侧需要安装 cryptography 包（requirements.txt 已包含）。
-- 若不想装，可改成旧插件（安全性降低，不推荐）：
-- CREATE USER 'sms_app'@'%' IDENTIFIED WITH mysql_native_password BY 'YourStrongPassword';


-- =====================================================================
-- 5. 建表结果自检（执行完上面的语句后运行，确认表与索引都在）
-- =====================================================================
-- USE `sms_monitor`;
-- SHOW CREATE TABLE `sms_records`;
-- SHOW INDEX FROM `sms_records`;
-- -- 确认新列已就位（应能看到 sender_name 与 sms_vcode 两行）：
-- SHOW COLUMNS FROM `sms_records` LIKE 'sender_name';
-- SHOW COLUMNS FROM `sms_records` LIKE 'sms_vcode';
-- -- 查看最近几条的解析结果，验证提取是否正常：
-- SELECT id, sender_name, sms_vcode, message_content
-- FROM `sms_records` ORDER BY id DESC LIMIT 5;
