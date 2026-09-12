import fs from 'fs';
import path from 'path';

import { buildIndicatorParityFixture } from './indicatorParityFixture';

const FIXTURE_PATH = path.resolve(__dirname, '../../../backend/tests/fixtures/stock_indicator_parity.json');

test('后端一致性夹具与当前前端指标算法的输出一致', () => {
  const fixture = JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf8'));
  // JSON 往返一次，和夹具文件的序列化方式对齐（undefined → 缺失）
  const current = JSON.parse(JSON.stringify(buildIndicatorParityFixture()));

  // 不一致说明前端指标算法改了：运行 node frontend/scripts/generate-indicator-parity-fixture.mjs
  // 重新生成夹具，并同步修改 backend/src/core/services/stock_system/indicators.py
  expect(current).toEqual(fixture);
});
