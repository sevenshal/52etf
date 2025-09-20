import React from 'react';
import { Row, Col, Statistic } from 'antd';
import { getCellColor } from '../utils';

const BondFearGreed = ({ 
  bondFearGreed, 
  us10y, 
  fedRateFrom, 
  fedRateTo, 
  forwardMin, 
  forwardMax, 
  forwardTable 
}) => {
  const getBondFearGreedColor = (value) => {
    if (value >= 75) return '#cf1322';
    if (value >= 55) return '#fa8c16';
    if (value >= 45) return '#d9d9d9';
    if (value >= 25) return '#52c41a';
    return '#237804';
  };

  return (
    <Row>
      <Col span={4} xs={24} sm={24} md={4} lg={4} xl={4}>
        <Statistic
          title="美债贪恐值"
          style={{ marginBottom: 16 }}
          value={
            bondFearGreed !== null
              ? `${bondFearGreed}/100${
                  bondFearGreed <= 30
                    ? ' (恐慌)'
                    : bondFearGreed >= 70
                    ? ' (贪婪)'
                    : ' (中性)'
                }`
              : '...'}
          valueStyle={{
            color: getBondFearGreedColor(bondFearGreed)
          }}
        />
        <div>10Y国债实时利率：{us10y || '...'}</div>
        <div>当前执行利率：{fedRateFrom} - {fedRateTo}</div>
        <div>未来一年预测利率：{forwardMin} - {forwardMax}</div>
      </Col>
      <Col span={20} xs={24} sm={24} md={20} lg={20} xl={20}>
        <div style={{ display: 'flex', flexDirection: 'row', alignItems: 'flex-start' }}>
          {/* 表格 */}
          <div style={{ minWidth: 320, overflowX: 'auto', marginRight: 16 }}>
            <table className="forward-table" style={{ borderCollapse: 'collapse', width: '100%' }}>
              <thead>
                <tr>
                  <th style={{ background: '#f0f0f0', position: 'sticky', left: 0, zIndex: 1, padding: '0 6px' }}>区间</th>
                  {forwardTable.columns.map(dateStr => (
                    <th
                      key={dateStr}
                      style={{
                        background: '#f0f0f0',
                        padding: '0 6px',
                        whiteSpace: 'nowrap'
                      }}
                    >
                      {dateStr}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {forwardTable.rows.slice().reverse().map(row => {
                  const cellColor = getCellColor(forwardTable);
                  return (
                    <tr key={row.rate}>
                      <td style={{ background: '#fafafa', position: 'sticky', left: 0, zIndex: 1, whiteSpace: 'nowrap', padding: '0 6px' }}>{row.rate}</td>
                      {forwardTable.columns.map(dateStr => (
                        <td key={dateStr} style={{...cellColor(row, dateStr), whiteSpace: 'nowrap', padding: '0 6px'}}>
                          {row[dateStr]}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </Col>
    </Row>
  );
};

export default BondFearGreed;
