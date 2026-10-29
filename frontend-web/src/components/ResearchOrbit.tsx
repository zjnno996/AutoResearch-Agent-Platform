export default function ResearchOrbit() {
  return (
    <svg
      viewBox="0 0 360 270"
      className="research-orbit"
      role="img"
      aria-label="资料、方案、实现、实验、报告和复核形成科研闭环"
    >
      <defs>
        <pattern
          id="orbit-grid"
          width="24"
          height="24"
          patternUnits="userSpaceOnUse"
        >
          <circle cx="1" cy="1" r=".7" fill="#91b8e2" opacity=".3" />
        </pattern>
      </defs>
      <rect width="360" height="270" fill="url(#orbit-grid)" />
      <circle
        cx="180"
        cy="135"
        r="92"
        fill="none"
        stroke="#89b2e0"
        strokeOpacity=".3"
        strokeDasharray="4 7"
      />
      <circle
        cx="180"
        cy="135"
        r="60"
        fill="none"
        stroke="#91b8e2"
        strokeOpacity=".12"
      />
      {[
        [180, 43],
        [260, 89],
        [260, 181],
        [180, 227],
        [100, 181],
        [100, 89],
      ].map(([x, y], i) => (
        <g key={i}>
          <path
            d={`M180 135 L${x} ${y}`}
            stroke="#9bc4f3"
            strokeOpacity=".28"
          />
          <circle
            cx={x}
            cy={y}
            r="20"
            fill="#113e71"
            stroke="#92bdeb"
            strokeOpacity=".6"
          />
          <text
            x={x}
            y={y + 4}
            textAnchor="middle"
            fill="#e6f0ff"
            fontSize="10"
            fontFamily="sans-serif"
          >
            {["资料", "方案", "实现", "实验", "报告", "复核"][i]}
          </text>
        </g>
      ))}
      <path
        d="m180 43 80 46v92l-80 46-80-46V89Z"
        fill="none"
        stroke="#7aaee4"
        strokeOpacity=".35"
      />
      <circle cx="180" cy="135" r="37" fill="#eaf3ff" />
      <path
        d="M165 147v-24l15-8 15 8v24l-15 8-15-8Zm0-24 15 12 15-12m-15 12v20"
        fill="none"
        stroke="#003f88"
        strokeWidth="1.6"
      />
      <circle cx="180" cy="135" r="3.5" fill="#003f88" />
      <text
        x="310"
        y="30"
        fill="#a7c5e7"
        fontSize="8"
        letterSpacing="2"
        textAnchor="end"
      >
        RESEARCH IN MOTION
      </text>
      <text
        x="310"
        y="253"
        fill="#a7c5e7"
        fontSize="8"
        letterSpacing="2"
        textAnchor="end"
      >
        EVIDENCE → DISCOVERY
      </text>
    </svg>
  );
}
