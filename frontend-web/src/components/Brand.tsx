export default function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="brand-lockup">
      <span className="brand-symbol" aria-hidden="true">
        <svg viewBox="0 0 36 36" fill="none">
          <path
            d="M7 25V11l11-6 11 6v14l-11 6-11-6Z"
            stroke="currentColor"
            strokeWidth="1.3"
          />
          <path
            d="m7 11 11 7 11-7M18 18v13M7 25l11-7 11 7"
            stroke="currentColor"
            strokeWidth="1.3"
          />
          {[
            [7, 11],
            [18, 5],
            [29, 11],
            [29, 25],
            [18, 31],
            [7, 25],
            [18, 18],
          ].map(([x, y], i) => (
            <circle
              key={i}
              cx={x}
              cy={y}
              r={i === 6 ? 3 : 2}
              fill="currentColor"
            />
          ))}
        </svg>
      </span>
      {!compact && (
        <span className="brand-wordmark">
          <strong>
            EMNETS<span>-RA</span>
          </strong>
          <small>科研助手 · Research Assistant</small>
        </span>
      )}
    </span>
  );
}
