"""HTTP-шар Order Desk: розбір запиту → виклик сервісу → рендер.

Правило межі (ARCHITECTURE_PLAN.md §2): роут бачить `Request` і будує
`Response`; усе, що цього не робить, живе в `app/services/`.
"""
