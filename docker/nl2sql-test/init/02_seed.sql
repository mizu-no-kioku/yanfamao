-- docker/nl2sql-test/init/02_seed.sql
-- 合成种子。覆盖 Review Focus 里的边界：一角色多菜单、跨租户用户、
-- 同租户多员工、无 AI 菜单角色、失效角色、悬空关系。

-- 菜单：253/254/238 为 AI 门，其余为普通菜单
INSERT INTO t_enterprise_menu (ID, OPERATER_STATUS, CODE, NAME) VALUES
  (253, 1, 'AI', 'AI助手'),
  (254, 1, 'QA', 'AI问答'),
  (238, 1, 'projectManage:projectList:ai', 'ai生成'),
  (900, 1, 'OTHER', '普通菜单'),
  (901, 1, 'OTHER2', '普通菜单2'),
  (902, 1, 'OTHER3', '普通菜单3');

-- 用户
INSERT INTO t_user (ID, OPERATER_STATUS) VALUES
  (1001, 1),   -- 正常：一角色、三菜单齐全
  (1002, 1),   -- 正常：一角色，无 AI 菜单（功能门负向）
  (1003, 1),   -- 一角色，恰好命中 1 个 AI 菜单（区分任意/全部规则）
  (1004, 1),   -- 跨租户：10 与 20 各一个角色
  (1005, 1),   -- 同租户两个有效员工（关联歧义）
  (1006, 0),   -- 用户失效
  (1007, 1);   -- 关系指向失效角色

-- 员工
INSERT INTO t_staff (ID, TENANT_ID, BELONG_USER_ID, OPERATER_STATUS) VALUES
  (2001, 10, 1001, 1),
  (2002, 10, 1002, 1),
  (2003, 10, 1003, 1),
  (2004, 10, 1004, 1),
  (2005, 20, 1004, 1),
  (2006, 10, 1005, 1),
  (2007, 10, 1005, 1),
  (2008, 10, 1006, 1),
  (2009, 10, 1007, 1);

-- 角色：3001 有效且三菜单；3002 有效但无 AI 菜单；3003 有效仅 1 个 AI 菜单；3004 失效
INSERT INTO t_enterprise_role (ID, TENANT_ID, OPERATER_STATUS) VALUES
  (3001, 10, 1), (3002, 10, 1), (3003, 10, 1), (3004, 10, 0),
  (3005, 20, 1);
INSERT INTO t_enterprise_role (ID, TENANT_ID, OPERATER_STATUS) VALUES (3006, 10, 1);

-- 角色—菜单：3001 关联大量菜单，用于证明「一角色多菜单仍是一个角色」
INSERT INTO tb_enterprise_role_menu_tenant (TENANT_ID, ENTERPRISE_ROLE_ID, ENTERPRISE_MENU_ID)
SELECT 10, 3001, ID FROM t_enterprise_menu;
INSERT INTO tb_enterprise_role_menu_tenant (TENANT_ID, ENTERPRISE_ROLE_ID, ENTERPRISE_MENU_ID) VALUES
  (10, 3002, 900), (10, 3002, 901),
  (10, 3003, 254),
  (10, 3006, 253), (10, 3006, 900),
  (20, 3005, 253), (20, 3005, 900);

-- 用户—角色—租户
INSERT INTO tb_user_enterprise_role_tenant (TENANT_ID, USER_ID, ENTERPRISE_ROLE_ID) VALUES
  (10, 1001, 3001),
  (10, 1002, 3002),
  (10, 1003, 3003),
  (10, 1004, 3001),
  (20, 1004, 3005),
  (10, 1005, 3001),
  (10, 1006, 3001),
  (10, 1007, 3004);   -- 指向失效角色

-- 项目与成员（租户 10）
INSERT INTO t_project (ID, TENANT_ID, PROJECT_NAME, GENERAL_BUDGET, OPERATER_STATUS) VALUES
  (5001, 10, '合成项目甲', 120000.00000, 1),
  (5002, 10, '合成项目乙', 340000.00000, 1),
  (5003, 10, '已失效项目', 50000.00000, 0),
  (5004, 20, '合成项目丙', 90000.00000, 1);   -- 租户 20：用于跨租户隔离
INSERT INTO t_project_user (ID, TENANT_ID, PROJECT_ID, STAFF_ID, OPERATER_STATUS) VALUES
  (6001, 10, 5001, 2001, 1),
  (6002, 10, 5002, 2001, 1),
  (6003, 10, 5003, 2001, 1),
  (6004, 20, 5004, 2005, 1);
