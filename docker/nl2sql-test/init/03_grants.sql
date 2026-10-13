-- docker/nl2sql-test/init/03_grants.sql
-- 账号供给随库初始化复现：应用账号只读、控制账号可写，与已提交的
-- TestDatabaseConfig 一致；重建容器后两个账号仍自动存在，无需手工创建。

CREATE USER IF NOT EXISTS 'nl2sql_app'@'%' IDENTIFIED BY 'app-only';
GRANT SELECT ON nl2sql_test.* TO 'nl2sql_app'@'%';

CREATE USER IF NOT EXISTS 'nl2sql_ctl'@'%' IDENTIFIED BY 'ctl-only';
GRANT ALL ON nl2sql_test.* TO 'nl2sql_ctl'@'%';

FLUSH PRIVILEGES;
