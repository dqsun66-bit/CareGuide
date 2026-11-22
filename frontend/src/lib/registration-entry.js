export const registrationToken = '#小程序://泰州人民医院互联网医院/1wGRDDgpEQNsJGg'

export function hasRegistrationGuide(message) {
  const guideUrl = 'https://www.jstzhospital.com/gwrk/xwzx/yyxw/content_1772'
  return message.workflow?.tasks?.some(task =>
    task.status === 'succeeded' &&
    task.arguments?.hospital_id === 'js_taizhou_001' &&
    task.arguments?.campus === 'js_taizhou_001_main' &&
    /挂号|挂.{0,12}的号|预约|实名建档/.test(task.label || '') &&
    task.result?.evidence?.some(source =>
      source.document_id === 'js_taizhou_001_appointment_0046' ||
      [source.source_url, source.url, source.source?.url, source.citation?.url].includes(guideUrl))
  ) ?? false
}
