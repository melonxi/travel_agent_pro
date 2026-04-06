import { MapContainer, TileLayer, Marker, Polyline } from 'react-leaflet'
import type { DayPlan } from '../types/plan'
import 'leaflet/dist/leaflet.css'

interface Props {
  dailyPlans: DayPlan[]
  dark: boolean
}

export default function MapView({ dailyPlans, dark }: Props) {
  const points = dailyPlans.flatMap((d) =>
    d.activities.map((a) => [a.location.lat, a.location.lng] as [number, number])
  )

  if (points.length === 0) {
    return (
      <div className="sidebar-section">
        <div className="section-title">路线</div>
        <div className="map-empty">
          <div className="map-empty-icon">◎</div>
          行程确定后将在此显示路线
        </div>
      </div>
    )
  }

  const center = points[0]
  const tileUrl = dark
    ? 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'
    : 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png'

  return (
    <div className="sidebar-section">
      <div className="section-title">路线</div>
      <div className="map-container">
        <MapContainer center={center} zoom={12} style={{ height: '260px', width: '100%' }} key={dark ? 'dark' : 'light'}>
          <TileLayer
            attribution='&copy; <a href="https://carto.com/">CARTO</a>'
            url={tileUrl}
          />
          {points.map((p, i) => (
            <Marker key={i} position={p} />
          ))}
          {points.length > 1 && (
            <Polyline
              positions={points}
              pathOptions={{ color: dark ? '#d4a853' : '#b8892e', weight: 2, opacity: 0.7, dashArray: '8,6' }}
            />
          )}
        </MapContainer>
      </div>
    </div>
  )
}
