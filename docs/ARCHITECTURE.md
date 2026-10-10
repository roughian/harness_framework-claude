# 아키텍처

## 디렉토리 구조
```
src/
├── app/               # 페이지 + API 라우트
├── components/        # UI 컴포넌트
├── types/             # TypeScript 타입 정의
├── lib/               # 유틸리티 + 헬퍼
└── services/          # 외부 API 래퍼
```

## 패턴
{사용하는 디자인 패턴 (예: Server Components 기본, 인터랙션이 필요한 곳만 Client Component)}

## 데이터 흐름
```
{데이터가 어떻게 흐르는지 (예:
사용자 입력 → Client Component → API Route → 외부 API → 응답 → UI 업데이트
)}
```

## 상태 관리
{상태 관리 방식 (예: 서버 상태는 Server Components, 클라이언트 상태는 useState/useReducer)}

## 공유 컨벤션
병렬 작업(여러 worktree·implementer 에이전트)을 시작하기 전에 오케스트레이터가 확정한다. 워커마다 다르게 판단할 여지를 없앤다.
- 명명: {예: 파일 kebab-case, 컴포넌트 PascalCase, API 경로 /api/v1/<복수명사>}
- 인터페이스: {예: 서비스 함수는 Result<T, AppError> 반환, 공용 타입은 types/에만}
- 에러 형식: {예: { error: { code, message } }}
