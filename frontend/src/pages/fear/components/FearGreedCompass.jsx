import React from 'react';

const FearGreedCompass = ({ score, rating }) => {
  const getRotation = (score) => {
    return -90 + (score * 180 / 100);
  };

  const getColor = (score) => {
    if (score >= 75) return '#cf1322';  // 极度贪婪
    if (score >= 55) return '#fa8c16';  // 贪婪
    if (score >= 45) return '#d9d9d9';  // 中性
    if (score >= 25) return '#52c41a';  // 恐惧
    return '#237804';  // 极度恐惧
  };

  // 计算刻度线位置的辅助函数
  const calculateTickPosition = (value) => {
    const angle = -180 + (value * 180 / 100);
    const radius = 70;
    const radian = (angle * Math.PI) / 180;
    return {
      x: 150 + radius * Math.cos(radian),
      y: 150 + radius * Math.sin(radian)
    };
  };

  return (
    <div style={{ position: 'relative', width: '300px', height: '170px', margin: '0 auto' }}>
      <svg width="300" height="170" style={{ position: 'absolute', top: 0, left: 0 }}>
        <defs>
          <linearGradient id="dialGradient" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" style={{ stopColor: '#237804' }} />
            <stop offset="25%" style={{ stopColor: '#52c41a' }} />
            <stop offset="45%" style={{ stopColor: '#d9d9d9' }} />
            <stop offset="55%" style={{ stopColor: '#d9d9d9' }} />
            <stop offset="75%" style={{ stopColor: '#fa8c16' }} />
            <stop offset="100%" style={{ stopColor: '#cf1322' }} />
          </linearGradient>
        </defs>
        
        {/* 表盘背景 */}
        <path 
          d="M 40 150 A 110 110 0 0 1 260 150" 
          fill="none" 
          stroke="url(#dialGradient)" 
          strokeWidth="25" 
        />

        {/* 刻度线和数字 */}
        {[0, 25, 50, 75, 100].map(value => {
          const pos = calculateTickPosition(value);
          const tickLength = 8;
          const labelOffset = 18;
          const angle = -180 + (value * 180 / 100);
          const radian = (angle * Math.PI) / 180;
          const cos = Math.cos(radian);
          const sin = Math.sin(radian);
          
          return (
            <g key={value}>
              <line
                x1={pos.x}
                y1={pos.y}
                x2={pos.x + cos * tickLength}
                y2={pos.y + sin * tickLength}
                stroke="#666"
                strokeWidth="2"
              />
              <text
                x={pos.x + cos * labelOffset}
                y={pos.y + sin * labelOffset}
                fill="#666"
                fontSize="12"
                textAnchor="middle"
                dominantBaseline="middle"
              >
                {value}
              </text>
            </g>
          );
        })}
      </svg>

      {/* 指针 */}
      <div style={{
        position: 'absolute',
        width: '3px',
        height: '110px',
        background: getColor(score),
        left: '150px',
        bottom: '20px',
        transform: `rotate(${getRotation(score)}deg)`,
        transformOrigin: 'bottom center',
        transition: 'transform 0.5s ease'
      }} />

      {/* 中心数值 */}
      <div style={{
        position: 'absolute',
        width: '100%',
        textAlign: 'center',
        bottom: '40px',
        fontSize: '36px',
        fontWeight: 'bold',
        color: getColor(score)
      }}>
        {Math.round(score)}
      </div>

      {/* 状态标签 */}
      <div style={{
        position: 'absolute',
        width: '100%',
        textAlign: 'center',
        bottom: '20px',
        fontSize: '14px',
        color: getColor(score)
      }}>
        {rating}
      </div>
    </div>
  );
};

export default FearGreedCompass;