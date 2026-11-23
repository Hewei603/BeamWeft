import {deferredPanel} from './deferredPanel';
// The canvas uses the same scientific inputs and capability browser before a study is opened.
import './ScientificStudies.css';

const loadStudies = () => import('./ScientificStudies');
export const ScientificStudyOutput = deferredPanel(loadStudies, '计算结果', 'ScientificStudyOutput');
export default deferredPanel(loadStudies, '分析记录');
