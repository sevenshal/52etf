import React from 'react';
import { Row, Col } from 'antd';
import FearGreedCompass from '../../FearGreedCompass';
import { getFearGreedColor, getFearGreedStatus } from '../utils';

const FearGreedCurrent = ({ fearGreedData }) => {
  if (!fearGreedData) return null;

  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} sm={24} md={8} lg={8} xl={8}>
        <FearGreedCompass 
          score={fearGreedData.fear_and_greed.score}
          rating={getFearGreedStatus(fearGreedData.fear_and_greed.score)}
        />
      </Col>
      <Col xs={24} sm={24} md={16} lg={16} xl={16}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 0' }}>
            <span style={{ color: '#666' }}>昨日收盘</span>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: '16px', fontWeight: 'bold', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_close) }}>
                {Math.round(fearGreedData.fear_and_greed.previous_close)}
              </div>
              <div style={{ fontSize: '12px', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_close) }}>
                {getFearGreedStatus(fearGreedData.fear_and_greed.previous_close)}
              </div>
            </div>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 0' }}>
            <span style={{ color: '#666' }}>一周前</span>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: '16px', fontWeight: 'bold', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_week) }}>
                {Math.round(fearGreedData.fear_and_greed.previous_1_week)}
              </div>
              <div style={{ fontSize: '12px', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_week) }}>
                {getFearGreedStatus(fearGreedData.fear_and_greed.previous_1_week)}
              </div>
            </div>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 0' }}>
            <span style={{ color: '#666' }}>一月前</span>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: '16px', fontWeight: 'bold', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_month) }}>
                {Math.round(fearGreedData.fear_and_greed.previous_1_month)}
              </div>
              <div style={{ fontSize: '12px', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_month) }}>
                {getFearGreedStatus(fearGreedData.fear_and_greed.previous_1_month)}
              </div>
            </div>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 0' }}>
            <span style={{ color: '#666' }}>一年前</span>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: '16px', fontWeight: 'bold', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_year) }}>
                {Math.round(fearGreedData.fear_and_greed.previous_1_year)}
              </div>
              <div style={{ fontSize: '12px', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_year) }}>
                {getFearGreedStatus(fearGreedData.fear_and_greed.previous_1_year)}
              </div>
            </div>
          </div>
        </div>
      </Col>
    </Row>
  );
};

export default FearGreedCurrent;
