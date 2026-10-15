export function formatDuration(seconds: number): string {
  if (seconds < 1) return "不足 1 秒";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  return [
    hours && `${hours} 小时`,
    minutes && `${minutes} 分`,
    rest && `${rest} 秒`,
  ]
    .filter(Boolean)
    .join(" ");
}
