/** spark-md5 无官方类型声明（仅 node 侧用作分片 md5）；按实际用法收窄。 */
declare module 'spark-md5' {
  interface SparkMD5Instance {
    append(data: ArrayBuffer | string): void
    end(raw?: boolean): string
    reset(): void
  }

  const SparkMD5: {
    ArrayBuffer: new () => SparkMD5Instance
    hash(data: ArrayBuffer | string, raw?: boolean): string
  }

  export default SparkMD5
}
