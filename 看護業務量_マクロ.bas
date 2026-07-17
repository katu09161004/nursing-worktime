Attribute VB_Name = "看護業務量_マクロ"
Option Explicit
'==================================================================
' 看護業務量集計シート ワンタップ記録マクロ
'   ・記録開始 → 業務が変わるたび 切替 → 最後に 記録終了
'   ・開始からの経過分を自動計算し、勤務時間内/外に振り分けて1行追加
'   使い方:
'     1) このブックを .xlsm で保存
'     2) Alt+F8 →「初期設定_ボタン作成」を一度だけ実行
'==================================================================

Private Const R_FIRST As Long = 7
Private Const R_LAST As Long = 126

' ---- 記録開始 ----
Public Sub 記録開始()
    Dim ws As Worksheet: Set ws = ThisWorkbook.Sheets("入力")
    If Trim(ws.Range("J5").Value) = "" Or Trim(ws.Range("J6").Value) = "" Then
        MsgBox "右上パネルで中分類・小分類を選んでから［記録開始］を押してください。", vbExclamation
        Exit Sub
    End If
    ws.Range("O1").Value = Now
    ws.Range("J7").Value = "記録中: " & ws.Range("J5").Value & " / " & ws.Range("J6").Value & _
                           "（開始 " & Format(Now, "hh:mm") & "）"
End Sub

' ---- 切替（今の業務を記録して、次の業務の計測を開始）----
Public Sub 切替()
    RecordInterval True
End Sub

' ---- 記録終了（今の業務を記録して停止）----
Public Sub 記録終了()
    RecordInterval False
End Sub

Private Sub RecordInterval(ByVal restart As Boolean)
    Dim ws As Worksheet: Set ws = ThisWorkbook.Sheets("入力")
    Dim startT As Variant: startT = ws.Range("O1").Value
    If Not IsDate(startT) Then
        MsgBox "先に［記録開始］を押してください。", vbExclamation
        Exit Sub
    End If
    Dim mid As String, sub_ As String
    mid = ws.Range("J5").Value: sub_ = ws.Range("J6").Value
    Dim inMin As Long, outMin As Long
    SplitInOut CDate(startT), Now, ws, inMin, outMin

    Dim r As Long: r = NextRow(ws)
    If r = 0 Then
        MsgBox "入力行(7～126)がいっぱいです。集計してからクリアしてください。", vbExclamation
        Exit Sub
    End If
    Application.EnableEvents = False
    ws.Cells(r, 2).Value = mid       ' B 中分類
    ws.Cells(r, 3).Value = sub_      ' C 小分類
    ws.Cells(r, 4).Value = inMin     ' D 勤務時間内(分)
    ws.Cells(r, 5).Value = outMin    ' E 勤務時間外(分)
    Application.EnableEvents = True
    ' A(大分類)・F(計) は数式で自動計算

    If restart Then
        ws.Range("O1").Value = Now
        ws.Range("J7").Value = "記録中: " & mid & " / " & sub_ & "（開始 " & Format(Now, "hh:mm") & "）"
    Else
        ws.Range("O1").ClearContents
        ws.Range("J7").Value = "停止中"
    End If
End Sub

' 勤務時間内/外に分割（B5=勤務開始, D5=勤務終了 を使用。夜勤の日跨ぎ対応）
Private Sub SplitInOut(ByVal s As Date, ByVal e As Date, ByVal ws As Worksheet, _
                       ByRef inMin As Long, ByRef outMin As Long)
    Dim total As Long
    total = CLng(Round((e - s) * 24# * 60#, 0))
    If total < 0 Then total = 0

    Dim wsStart As Variant, wsEnd As Variant
    wsStart = ws.Range("B5").Value: wsEnd = ws.Range("D5").Value
    If Not (IsDate(wsStart) And IsDate(wsEnd)) Then
        inMin = total: outMin = 0: Exit Sub   ' 勤務時間未設定なら全て内扱い
    End If

    Dim d As Date: d = Int(s)
    Dim wStart As Date, wEnd As Date
    wStart = d + TimeValue(Format(wsStart, "hh:mm"))
    wEnd = d + TimeValue(Format(wsEnd, "hh:mm"))
    If wEnd <= wStart Then wEnd = wEnd + 1     ' 夜勤：翌日にまたぐ

    Dim ovS As Date, ovE As Date
    ovS = IIf(s > wStart, s, wStart)
    ovE = IIf(e < wEnd, e, wEnd)
    Dim within As Long: within = 0
    If ovE > ovS Then within = CLng(Round((ovE - ovS) * 24# * 60#, 0))
    If within > total Then within = total
    If within < 0 Then within = 0
    inMin = within
    outMin = total - within
End Sub

Private Function NextRow(ByVal ws As Worksheet) As Long
    Dim r As Long
    For r = R_FIRST To R_LAST
        If Trim(ws.Cells(r, 2).Value) = "" Then NextRow = r: Exit Function
    Next r
    NextRow = 0
End Function

' ---- 入力クリア ----
Public Sub クリア入力()
    Dim ws As Worksheet: Set ws = ThisWorkbook.Sheets("入力")
    If MsgBox("入力データ（中分類・小分類・分数・備考）を消去します。よろしいですか？", _
              vbYesNo + vbQuestion) <> vbYes Then Exit Sub
    Application.EnableEvents = False
    ws.Range("B7:E126").ClearContents
    ws.Range("G7:G126").ClearContents
    ws.Range("O1").ClearContents
    ws.Range("J7").Value = "停止中"
    Application.EnableEvents = True
End Sub

' ---- 集計シートへ ----
Public Sub 集計へ()
    ThisWorkbook.Sheets("集計").Activate
End Sub

' ---- 初期設定：ボタンを作成して割り当て（1回だけ実行）----
Public Sub 初期設定_ボタン作成()
    Dim ws As Worksheet: Set ws = ThisWorkbook.Sheets("入力")
    Dim shp As Shape
    On Error Resume Next
    For Each shp In ws.Shapes
        If Left(shp.Name, 3) = "btn" Then shp.Delete
    Next shp
    On Error GoTo 0

    Dim names As Variant, caps As Variant, acts As Variant, i As Long
    names = Array("btn記録開始", "btn切替", "btn記録終了", "btnクリア", "btn集計", "btn送信")
    caps = Array("記録開始", "切替（次の業務へ）", "記録終了", "クリア", "集計へ", "サーバへ送信")
    acts = Array("記録開始", "切替", "記録終了", "クリア入力", "集計へ", "送信")

    Dim leftPos As Double, topPos As Double, w As Double, h As Double
    leftPos = ws.Range("I9").Left
    topPos = ws.Range("I9").Top
    w = 120: h = 28
    For i = 0 To UBound(names)
        Dim b As Button
        Set b = ws.Buttons.Add(leftPos, topPos + i * (h + 4), w, h)
        b.Name = names(i)
        b.Caption = caps(i)
        b.OnAction = acts(i)
    Next i
    MsgBox "ボタンを作成しました。右上パネルの下に並んでいます。", vbInformation
End Sub

'==================================================================
' サーバ同期（オンライン/オフライン自動切替）
'   ・オンライン同期=ON かつ サーバに到達可 → /api/entries/bulk へ送信
'   ・到達不可 or OFF → 未送信フラグを立てて保留（持ち出し時など）
'   ・再接続後は 送信 または ブック起動時に自動フラッシュ
'   設定セル（入力シート）:
'     N3 = APIのURL（例 http://192.168.0.10:8300）
'     N4 = オンライン同期 ON/OFF
'     N5 = 状態表示（自動更新）
'     N6 = 未送信フラグ（1=未送信）内部用
'==================================================================

Private Function ApiBase() As String
    ApiBase = Trim(CStr(ThisWorkbook.Sheets("入力").Range("N3").Value))
    If Right(ApiBase, 1) = "/" Then ApiBase = Left(ApiBase, Len(ApiBase) - 1)
End Function

Private Function SyncOn() As Boolean
    SyncOn = (UCase(Trim(CStr(ThisWorkbook.Sheets("入力").Range("N4").Value))) <> "OFF")
End Function

Private Sub SetStatus(ByVal msg As String)
    ThisWorkbook.Sheets("入力").Range("N5").Value = msg
End Sub

' サーバに到達できるか（/health を短時間で叩く）
Public Function IsOnline() As Boolean
    On Error GoTo NG
    If ApiBase() = "" Then IsOnline = False: Exit Function
    Dim http As Object
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.SetTimeouts 1500, 1500, 2000, 2000
    http.Open "GET", ApiBase() & "/health", False
    http.Send
    IsOnline = (http.Status = 200)
    Exit Function
NG:
    IsOnline = False
End Function

Private Function JsonEsc(ByVal s As String) As String
    s = Replace(s, "\", "\\")
    s = Replace(s, """", "\""")
    s = Replace(s, vbCr, " "): s = Replace(s, vbLf, " ")
    JsonEsc = s
End Function

' 入力シートから一括投入用JSONを組み立てる
Private Function BuildBatchJson() As String
    Dim ws As Worksheet: Set ws = ThisWorkbook.Sheets("入力")
    Dim staff As String, ward As String, shift As String, dt As String
    ward = Trim(CStr(ws.Range("B4").Value))
    staff = Trim(CStr(ws.Range("D4").Value))
    shift = Trim(CStr(ws.Range("F4").Value))
    If IsDate(ws.Range("H4").Value) Then
        dt = Format(ws.Range("H4").Value, "yyyy-mm-dd")
    Else
        dt = Format(Date, "yyyy-mm-dd")
    End If

    Dim rows As String, r As Long, mid As String, sub_ As String, ins As Double, outs As Double
    rows = ""
    For r = 7 To 126
        mid = Trim(CStr(ws.Cells(r, 2).Value))   ' B 中分類
        If mid <> "" Then
            sub_ = Trim(CStr(ws.Cells(r, 3).Value))  ' C 小分類
            ins = Val(ws.Cells(r, 4).Value)          ' D 内
            outs = Val(ws.Cells(r, 5).Value)         ' E 外
            If ins > 0 Or outs > 0 Then
                If rows <> "" Then rows = rows & ","
                rows = rows & "{""mid"":""" & JsonEsc(mid) & """,""sub"":""" & JsonEsc(sub_) & _
                       """,""in"":" & CLng(ins) & ",""out"":" & CLng(outs) & "}"
            End If
        End If
    Next r

    BuildBatchJson = "{""staff_id"":""" & JsonEsc(staff) & """,""ward"":""" & JsonEsc(ward) & _
                     """,""shift"":""" & JsonEsc(shift) & """,""date"":""" & dt & _
                     """,""rows"":[" & rows & "]}"
End Function

' UTF-8でPOST（日本語ラベルが化けないよう ADODB.Stream で符号化）
Private Function HttpPostJson(ByVal url As String, ByVal body As String) As Long
    On Error GoTo NG
    Dim http As Object, st As Object
    Set st = CreateObject("ADODB.Stream")
    st.Type = 2: st.Charset = "utf-8": st.Open: st.WriteText body
    st.Position = 0: st.Type = 1
    Dim bytes() As Byte: bytes = st.Read: st.Close
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.SetTimeouts 2000, 2000, 5000, 8000
    http.Open "POST", url, False
    http.SetRequestHeader "Content-Type", "application/json; charset=utf-8"
    http.Send bytes
    HttpPostJson = http.Status
    Exit Function
NG:
    HttpPostJson = 0
End Function

' 送信：オンラインなら送る／不可なら未送信フラグを立てる
Public Sub 送信()
    Dim ws As Worksheet: Set ws = ThisWorkbook.Sheets("入力")
    If ApiBase() = "" Then
        MsgBox "入力シートの N3 にサーバのURL（例 http://192.168.0.10:8300）を入れてください。", vbExclamation
        Exit Sub
    End If
    If Not SyncOn() Then
        ws.Range("N6").Value = "1": SetStatus "オフライン設定（未送信・保留中）"
        MsgBox "オンライン同期がOFFです。持ち出しモードとして保留しました。ONにして再送してください。", vbInformation
        Exit Sub
    End If
    If Not IsOnline() Then
        ws.Range("N6").Value = "1": SetStatus "オフライン（未送信・再接続時に自動送信）"
        MsgBox "サーバに接続できませんでした。オフラインとして保留しました。", vbInformation
        Exit Sub
    End If
    Dim code As Long
    code = HttpPostJson(ApiBase() & "/api/entries/bulk", BuildBatchJson())
    If code = 200 Then
        ws.Range("N6").Value = "0": SetStatus "送信済 " & Format(Now, "mm/dd hh:mm")
        MsgBox "サーバに送信しました。", vbInformation
    Else
        ws.Range("N6").Value = "1": SetStatus "送信失敗(" & code & ")・未送信"
        MsgBox "送信に失敗しました（コード " & code & "）。保留しました。", vbExclamation
    End If
End Sub

' 未送信を自動送信（ブック起動時などに呼ぶ）
Public Sub 同期_未送信を送る()
    Dim ws As Worksheet: Set ws = ThisWorkbook.Sheets("入力")
    If Not SyncOn() Then Exit Sub
    If CStr(ws.Range("N6").Value) <> "1" Then Exit Sub   ' 未送信が無ければ何もしない
    If Not IsOnline() Then SetStatus "オフライン（未送信）": Exit Sub
    Dim code As Long
    code = HttpPostJson(ApiBase() & "/api/entries/bulk", BuildBatchJson())
    If code = 200 Then
        ws.Range("N6").Value = "0": SetStatus "自動送信済 " & Format(Now, "mm/dd hh:mm")
    End If
End Sub

' 状態を確認して表示だけ更新
Public Sub 状態更新()
    If Not SyncOn() Then SetStatus "オフライン設定": Exit Sub
    If IsOnline() Then
        SetStatus IIf(CStr(ThisWorkbook.Sheets("入力").Range("N6").Value) = "1", "オンライン（未送信あり）", "オンライン")
    Else
        SetStatus "オフライン"
    End If
End Sub
